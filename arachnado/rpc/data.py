import logging
import json
from collections import deque
from tornado import gen
import tornado.ioloop
from bson.objectid import ObjectId
from bson.errors import InvalidId
from jsonrpc.dispatcher import Dispatcher

from arachnado.rpc.jobs import Jobs
from arachnado.rpc.sites import Sites
from arachnado.rpc.pages import Pages

from arachnado.crawler_process import agg_stats_changed, CrawlerProcessSignals as CPS
from arachnado.rpc.ws import RpcWebsocketHandler
from arachnado.utils.misc import json_encode

logger = logging.getLogger(__name__)


class DataRpcWebsocketHandler(RpcWebsocketHandler):
    """ basic class for Data API handlers"""
    stored_data = deque()
    delay_mode = False
    event_types = []
    data_hb = None
    i_args = None
    i_kwargs = None
    storages = {}
    # TODO: allow client to update this
    max_msg_size = 2**20

    def _send_event(self, event, data):
        message = json_encode({'event': event, 'data': data})
        if len(message) < self.max_msg_size:
            # logging.info("{}: {}: {}".format(self.cnt, event, len(message)))
            # self.cnt += 1
            return super(DataRpcWebsocketHandler, self).write_event(event, data)

    def init_hb(self, update_delay):
        if update_delay > 0 and not self.data_hb:
            self.delay_mode = True
            self.data_hb = tornado.ioloop.PeriodicCallback(
                lambda: self.send_updates(),
                update_delay
            )
            self.data_hb.start()

    def add_storage_wrapper(self, mongo_q, storage_wrapper):
        self.dispatcher.add_object(storage_wrapper)
        new_id = str(len(self.storages))
        self.storages[new_id] = {
            "storage": storage_wrapper,
            "job_ids": set([])
        }
        storage_wrapper.handler_id = new_id
        storage_wrapper.subscribe(query=mongo_q)
        return new_id

    def cancel_subscription(self, subscription_id):
        storage = self.storages.pop(subscription_id, None)
        if storage:
            storage._on_close()
            return True
        else:
            return False

    def initialize(self, *args, **kwargs):
        self.i_args = args
        self.i_kwargs = kwargs
        self.cp = kwargs.get("crawler_process", None)
        self.dispatcher = Dispatcher()
        self.dispatcher["cancel_subscription"] = self.cancel_subscription

    def on_close(self):
        logger.info("connection closed")
        for storage in self.storages.values():
            storage["storage"]._on_close()
        if self.data_hb:
            self.data_hb.stop()
        super(DataRpcWebsocketHandler, self).on_close()

    def open(self):
        logger.info("new connection")
        super(DataRpcWebsocketHandler, self).open()

    def on_spider_closed(self, spider):
        if self.cp:
            for job in self.cp.jobs:
                self.write_event("jobs:state", job)

    def send_updates(self):
        while len(self.stored_data):
            item = self.stored_data.popleft()
            return self._send_event(item["event"], item["data"])


class JobsDataRpcWebsocketHandler(DataRpcWebsocketHandler):
    event_types = ['stats:changed',]
    mongo_id_mapping = {}
    job_url_mapping = {}

    def subscribe_to_jobs(self, include=[], exclude=[], update_delay=0):
        mongo_q = self.create_jobs_query(include=include, exclude=exclude)
        self.init_hb(update_delay)
        return { "datatype": "job_subscription_id",
            "id": self.add_storage_wrapper(mongo_q, storage_wrapper=self.create_jobs_storage_link())
        }

    @gen.coroutine
    def write_event(self, event, data, handler_id=None):
        event_data = data
        if event == 'jobs.tailed' and "id" in data and handler_id:
            self.storages[handler_id]["job_ids"].add(data["id"])
            self.mongo_id_mapping[data["id"]] = data.get("_id", None)
            self.job_url_mapping[data["id"]] = data.get("urls", None)
        if event in ['stats:changed', 'jobs:state']:
            job_id = None
            if event == 'stats:changed':
                if len(data) > 1:
                    job_id = data[0]
                    # two fields with same content for back compatibility
                    event_data = {"stats": data[1],
                                  "stats_dict": data[1],
                                  }
                    # same as crawl_id
                    event_data["id"] = job_id
                    # mongo id
                    event_data["_id"] = self.mongo_id_mapping.get(job_id, "")
                    # job url
                    event_data["urls"] = self.job_url_mapping.get(job_id, "")
            else:
                job_id = data["id"]
            allowed = False
            if job_id:
                for storage in self.storages.values():
                    allowed = allowed or job_id in storage["job_ids"]
            if not allowed:
                return
        if 'stats' in event_data:
            if not isinstance(event_data['stats'], dict):
                try:
                    event_data['stats'] = json.loads(event_data['stats'])
                except Exception as ex:
                    logger.warning("Invalid stats field in job {}".format(event_data.get("_id", "MISSING MONGO ID")))
        if event in self.event_types and self.delay_mode:
            self.stored_data.append({"event":event, "data":event_data})
        else:
            return self._send_event(event, event_data)

    def create_jobs_query(self, include, exclude):
        conditions = []
        for inc_str in include:
            conditions.append({"urls":{'$regex': '.*' + inc_str + '.*'}})
        for exc_str in exclude:
            conditions.append({"urls":{'$regex': '^((?!' + exc_str + ').)*$'}})
        jobs_q = {}
        if len(conditions) == 1:
            jobs_q = conditions[0]
        elif len(conditions):
            jobs_q = {"$and": conditions }
        return jobs_q

    def initialize(self, *args, **kwargs):
        super(JobsDataRpcWebsocketHandler, self).initialize(*args, **kwargs)
        self.dispatcher["subscribe_to_jobs"] = self.subscribe_to_jobs

    def create_jobs_storage_link(self):
        jobs = Jobs(self, *self.i_args, **self.i_kwargs)
        return jobs

    def on_close(self):
        logger.info("connection closed")
        if self.cp:
            self.cp.signals.disconnect(self.on_stats_changed, agg_stats_changed)
            self.cp.signals.disconnect(self.on_spider_closed, CPS.spider_closed)
        super(JobsDataRpcWebsocketHandler, self).on_close()

    def open(self):
        logger.info("new connection")
        super(JobsDataRpcWebsocketHandler, self).open()
        if self.cp:
            self.cp.signals.connect(self.on_stats_changed, agg_stats_changed)
            self.cp.signals.connect(self.on_spider_closed, CPS.spider_closed)

    def on_stats_changed(self, changes, crawler):
        crawl_id = crawler.spider.crawl_id
        self.write_event("stats:changed", [crawl_id, changes])


class PagesDataRpcWebsocketHandler(DataRpcWebsocketHandler):
    """ pages API"""
    event_types = ['pages.tailed']

    def subscribe_to_pages(self, site_ids={}, update_delay=0, mode="urls"):
        self.init_hb(update_delay)
        result = {
            "datatype": "pages_subscription_id",
            "single_subscription_id": "",
            "id": {},
        }
        if mode == "urls":
            mongo_q = self.create_pages_query(site_ids=site_ids)
            result["single_subscription_id"] = self.add_storage_wrapper(mongo_q, storage_wrapper=self.create_pages_storage_link())
        elif mode == "ids":
            res = {}
            for site_id in site_ids:
                mongo_q = self.create_pages_query(site_ids=site_ids[site_id])
                res[site_id] = self.add_storage_wrapper(mongo_q, storage_wrapper=self.create_pages_storage_link())
            result["id"] = res
        return result

    @gen.coroutine
    def write_event(self, event, data, handler_id=None):
        if event in self.event_types and self.delay_mode:
            self.stored_data.append({"event":event, "data":data})
        else:
            return self._send_event(event, data)

    def initialize(self, *args, **kwargs):
        super(PagesDataRpcWebsocketHandler, self).initialize(*args, **kwargs)
        self.dispatcher["subscribe_to_pages"] = self.subscribe_to_pages

    def create_pages_storage_link(self):
        pages = Pages(self, *self.i_args, **self.i_kwargs)
        return pages

    def create_pages_query(self, site_ids):
        conditions = []
        if site_ids:
            for site in site_ids:
                url_only = False
                url_field_name = "url"
                if site_ids[site]:
                    if "url_field" in site_ids[site]:
                        url_field_name = site_ids[site]["url_field"]
                        item_id = site_ids[site]["id"]
                    else:
                        item_id = site_ids[site]
                    try:
                        item_id = ObjectId(item_id)
                        conditions.append(
                            {"$and":[{url_field_name:{"$regex": site + '.*'}},
                                {"_id":{"$gt":item_id}}
                            ]}
                        )
                    except InvalidId:
                        logger.warning("Invlaid ObjectID: {}, will use url condition only.".format(item_id))
                        url_only = True
                else:
                    url_only = True
                if url_only:
                    conditions.append(
                        {url_field_name:{"$regex": site + '.*'}}
                    )
        items_q = {}
        if len(conditions) == 1:
            items_q = conditions[0]
        elif len(conditions):
            items_q = {"$or": conditions}
        return items_q
