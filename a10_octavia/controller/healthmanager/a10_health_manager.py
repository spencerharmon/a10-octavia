# Copyright 2019 A10 Networks.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#

from concurrent import futures
import datetime
import time

from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
from oslo_utils import excutils

from octavia.controller.healthmanager import health_manager
from octavia.db import api as db_apis

from a10_octavia.controller.worker import controller_worker as cw
from a10_octavia.db import repositories as a10repo

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class A10HealthManager(health_manager.HealthManager):
    def __init__(self, exit_event):
        super(A10HealthManager, self).__init__(exit_event)
        self.cw = cw.A10ControllerWorker()
        self.threads = CONF.a10_health_manager.failover_threads
        self.executor = futures.ThreadPoolExecutor(max_workers=self.threads)
        self.vthunder_repo = a10repo.VThunderRepository()
        self.dead = exit_event

    def health_check(self):
        LOG.debug('health_check() starting...')
        futs = []
        while not self.dead.is_set():
            vthunder = None
            lock_session = None
            try:
                lock_session = db_apis.get_session(autocommit=False)
                failover_wait_time = datetime.datetime.utcnow() - datetime.timedelta(
                    seconds=CONF.a10_health_manager.heartbeat_timeout)
                initial_setup_wait_time = datetime.datetime.utcnow() - datetime.timedelta(
                    seconds=CONF.a10_health_manager.failover_timeout)
                vthunder = self.vthunder_repo.get_stale_vthunders(
                    lock_session, initial_setup_wait_time, failover_wait_time)
                if vthunder:
                    self.vthunder_repo.set_vthunder_health_state(
                        lock_session, vthunder.id, 'DOWN')
                lock_session.commit()

            except db_exc.DBDeadlock:
                LOG.debug('Database reports deadlock. Skipping.')
                lock_session.rollback()
            except db_exc.RetryRequest:
                LOG.debug('Database is requesting a retry. Skipping.')
                lock_session.rollback()
            except db_exc.DBConnectionError:
                db_apis.wait_for_connection(self.dead)
                lock_session.rollback()
                if not self.dead.is_set():
                    time.sleep(CONF.health_manager.heartbeat_timeout)
            except Exception as e:
                with excutils.save_and_reraise_exception():
                    LOG.debug("Database error while health_check: %s", str(e))
                    if lock_session:
                        lock_session.rollback()

            if vthunder is None:
                break

            LOG.info("Stale vThunder's id is: %s", vthunder.vthunder_id)
            fut = self.executor.submit(self.cw.failover_amphora, vthunder.vthunder_id)
            futs.append(fut)
            if len(futs) == self.threads:
                break

        if futs:
            LOG.info("Waiting for %s failovers to finish",
                     len(futs))
            health_manager.wait_done_or_dead(futs, self.dead)
            LOG.info("Successfully completed failover for VThunders.")
