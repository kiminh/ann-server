import falcon
from annoy import AnnoyIndex
import json
from time import time
from typing import Dict, List, Union, Any, Optional
import boto3
import s3fs
import datetime
from pathlib import Path
from ..io import needs_reload, load_via_tar, load_index, get_dynamo_emb
import logging

logging.basicConfig(level=logging.INFO)

S3_URI_PREFIX = 's3://'


PATH_TMP = Path('/tmp/ann')
PATH_TMP.parent.mkdir(parents=True, exist_ok=True)
ANN_INDEX_KEY = 'index.ann'
ANN_IDS_KEY = 'ids.txt'
ANN_META_KEY = 'metadata.json'
TIMESTAMP_LOCAL_KEY = 'timestamp.txt'
DYNAMO_ID = 'variant_id'
DYNAMO_KEY = 'repr'
DTYPE_FMT = 'f'  # float32 struct
SEED = 322

PathType = Union[Path, str]

s3 = s3fs.S3FileSystem()
dynamodb = boto3.resource('dynamodb')


class Rec(object):

    def __init__(self, id_, dist=None):
        self.id_ = id_
        self.dist = dist

    @property
    def score(self):
        """
        NOTE: This is only for ANNOY's angular distance (which is [0, 2])
        https://github.com/spotify/annoy/issues/149"""

        if self.dist is None:
            return None
        else:
            return 1. - self.dist / 2.

    def to_dict(self, incl_dist=True, incl_score=True):
        d = {'id': self.id_}
        if incl_dist:
            d['dist'] = self.dist
        if incl_score:
            d['score'] = self.score
        return d


class ANNResource(object):

    def __init__(self, path_tar: PathType,
                 ooi_dynamo_table: dynamodb.Table = None,
                 name: str = None,
                 ):
        """

        Args:
            path_tar: path to tar file with ann index and metadata
            ooi_dynamo_table: dynamo table for out of index lookup
            name:
        """
        self.path_tar = path_tar
        self.ooi_dynamo_table = ooi_dynamo_table
        self.name = name

        # not multithread-safe to do this with multiple indexes per server
        # self.index: AnnoyIndex = None

        self.path_index_local: str = None
        self.ids: List[Any] = None
        self.ids_d: Dict[Any, int] = None
        self.ann_meta_d: Dict[str, Any] = None
        self.fallback_parent: 'ANNResource' = None
        self.ooi_ann: 'ANNResource' = None

        # There is a chance that the ANN is already downloaded in tmp
        self.load(reload=needs_reload(self.path_tar, self.ts_read_utc))

    @property
    def path_extract(self) -> PathType:
        ann_name = self.name or Path(self.path_tar).stem.split('.')[0]
        return PATH_TMP / ann_name

    @property
    def ts_read_utc(self) -> Optional[datetime.datetime]:
        path_local_ts_read = self.path_extract / TIMESTAMP_LOCAL_KEY
        if not Path(path_local_ts_read).exists():
            local_mtime = None
        else:
            local_mtime = datetime.datetime.fromtimestamp(
                int(open(path_local_ts_read, 'r').read().strip()),
                tz=datetime.timezone.utc)
        return local_mtime

    @property
    def needs_reload(self):
        return needs_reload(self.path_tar, self.ts_read_utc)

    @property
    def ann_index(self) -> AnnoyIndex:
        return load_index(self.path_index_local, self.ann_meta_d)

    def load(self, path_tar: str = None, reload: bool = True):
        path_tar = path_tar or self.path_tar
        tic = time()
        logging.info(f'Loading: {path_tar}')
        self.path_index_local, self.ids, self.ids_d, \
            ts_read, self.ann_meta_d = \
            load_via_tar(path_tar, self.path_extract, reload)
        logging.info(f'...Done Loading! [{time() - tic} s]')

    def maybe_reload(self):
        if self.needs_reload:
            logging.info(f'Reloading [{self.path_tar}] due to staleness')
            self.load(reload=True)

    def recs_via_ann_out(self, ann_out, incl_dist) -> List[Rec]:
        """Convenience fn for constructing rec objects
        from ann output
        """
        if incl_dist:
            inds, dists = ann_out
        else:
            inds = ann_out
            dists = [None] * len(inds)
        ids = [self.ids[ind] for ind in inds]
        neighbors = [
            Rec(id_, dist) for id_, dist in zip(ids, dists)
        ]
        return neighbors

    def nn_from_emb(self, q_emb, k: int, ann_index=None, incl_dist=False
                    ) -> List[Rec]:
        ann_index = ann_index or self.ann_index
        ann_out = ann_index.get_nns_by_vector(
            q_emb, k, include_distances=incl_dist)
        neighbors = self.recs_via_ann_out(ann_out, incl_dist)
        return neighbors

    def nn_from_id(self, q_id: str, k: int, ann_index=None, incl_dist=False):
        ann_index = ann_index or self.ann_index
        if q_id in self.ids_d:
            q_ind = self.ids_d[q_id]
            # Note: if id in index, query 1 more than you need and discard 1st

            ann_out = ann_index.get_nns_by_item(
                q_ind, k + 1, include_distances=incl_dist)
            neighbors = self.recs_via_ann_out(ann_out, incl_dist)

        elif self.ooi_dynamo_table is not None:
            # Need to look up the vector and query by vector
            q_emb = get_dynamo_emb(self.ooi_dynamo_table, q_id)
            if q_emb is None:
                raise Exception(
                    'Q is ooi and doesnt exist in the ooi dynamo table')
            neighbors = self.nn_from_emb(
                q_emb, k, ann_index=ann_index, incl_dist=incl_dist)
        elif self.ooi_ann is not None:
            # Need to look up the vector and query by vector
            q_emb = self.ooi_ann.get_vector(q_id)
            if q_emb is None:
                raise Exception(
                    'Q is ooi and doesnt exist in the ooi ann')
            neighbors = self.nn_from_emb(
                q_emb, k, ann_index=ann_index, incl_dist=incl_dist)
        else:
            # TODO: there's a chance Q is in the fallback parent index
            # TODO: depending on how the indexes were created
            raise Exception('Q is ooi and no ooi dynamo table was set')

        neighbors = [n for n in neighbors if n.id_ != q_id]

        return neighbors

    def nn_from_payload(self, payload: Dict) -> List[Rec]:
        # TODO: parse and use `search_k`
        k = payload['k']
        incl_dist = payload.get('incl_dist') or False
        incl_score = bool(payload.get('incl_score')) or False
        thresh_score = payload.get('thresh_score')
        thresh_score = float(thresh_score) if thresh_score else False
        include_distances = bool(incl_dist or incl_score or thresh_score)

        if 'id' in payload:
            q_id = payload['id']
            neighbors = self.nn_from_id(
                q_id, k, incl_dist=include_distances)
        elif 'emb' in payload:
            q_emb = payload['emb']
            neighbors = self.nn_from_emb(
                q_emb, k, incl_dist=include_distances)
        else:
            raise Exception('Payload must contain `id` or `emb`')

        # Fallback lookup if not enough neighbors
        # TODO: there are some duplicated overheads by calling this
        if (len(neighbors) < k) and (self.fallback_parent is not None):
            neighbors_fallback = self.fallback_parent.nn_from_payload(
                {**payload, **{'k': k - len(neighbors)}}
            )
            neighbors += neighbors_fallback

        if thresh_score:
            neighbors = [n for n in neighbors if n.score > thresh_score]

        return neighbors[:k]

    def get_vector(self, q_id):
        if q_id in self.ids_d:
            q_ind = self.ids_d[q_id]
            ann_index = self.ann_index
            q_emb = ann_index.get_item_vector(q_ind)
        elif self.ooi_dynamo_table is not None:
            q_emb = get_dynamo_emb(self.ooi_dynamo_table, q_id)
        elif self.ooi_ann is not None:
            q_emb = self.ooi_ann.get_vector(q_id)
        else:
            return None

        return q_emb

    def on_post(self, req, resp):
        try:
            payload_json_buf = req.bounded_stream
            payload_json = json.load(payload_json_buf)

            neighbors = self.nn_from_payload(payload_json)
            incl_dist = bool(payload_json.get('incl_dist')) or False
            incl_score = bool(payload_json.get('incl_score')) or False
            recs = [n.to_dict(incl_dist, incl_score) for n in neighbors]

            res = {
                'recs': recs,
                'id_type': '-',
            }

            resp.body = json.dumps(res)
            resp.status = falcon.HTTP_200
        except Exception as e:
            # resp.body = json.dumps(
            #     {'Error': f'An internal server error has occurred:\n{e}'})
            # resp.status = falcon.HTTP_500
            print(e)
            # Return empty response with 200
            resp.body = json.dumps([])
            resp.status = falcon.HTTP_200

    def on_get(self, req, resp):
        """Retrieve vector for given id
        If the id exists in index, grab from index.
        If not (item is not active or something), try grabbing from dynamo.
        TODO:
        Finally, if desired, calculate the cold embedding somehow
        """

        q_id = req.params['id']

        q_emb = self.get_vector(q_id)

        if q_emb is None:
            resp.status = falcon.HTTP_200
        else:
            resp.status = falcon.HTTP_200
            resp.body = json.dumps(q_emb)

    def set_fallback(self, fallback_parent: 'ANNResource'):
        self.fallback_parent = fallback_parent

    def tojson(self):
        return {
            'path_tar': self.path_tar,
            'ann_meta': self.ann_meta_d,
            'ts_read': self.ts_read_utc.isoformat(),
            'n_ids': len(self.ids),
            'head5_ids': self.ids[:5],
        }


def dist_to_score(l, score_thresh=float('-inf')):
    """
        Adds a key 'score' to a list of dictionaries with distance
    NOTE: This is only for ANNOY's angular distance (which is [0, 2])
    https://github.com/spotify/annoy/issues/149

    Args:
        d: List of neighbor dicts [{'id': id, 'distance': 0.5}, ...]
        score_thresh: threshold to filter scores on

    Returns: Dict of scores (where higher is better)

    """
    if score_thresh is False:
        score_thresh = float('-inf')

    return [{'score': (1. - d['distance'] / 2.), **d}
            for d in l
            if (1. - d['distance'] / 2.) > score_thresh
            ]
