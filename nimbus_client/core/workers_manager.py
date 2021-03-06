#!/usr/bin/python
"""
Copyright (C) 2012 Konstantin Andrusenko
    See the documentation for further information on copyrights,
    or contact the author. All Rights Reserved.

@package nimbus_client.core.workers_manager
@author Konstantin Andrusenko
@date October 24, 2012

This module contains the implementation of PutWorker, GetWorker and WorkersManager classes
"""
import time
import threading
from Queue import Queue

from nimbus_client.core.constants import FG_ERROR_TIMEOUT
from nimbus_client.core.logger import logger
from nimbus_client.core.events import events_provider

QUIT_JOB = None

class PutWorker(threading.Thread):
    def __init__(self, fabnet_gateway, transactions_manager):
        threading.Thread.__init__(self)
        self.fabnet_gateway = fabnet_gateway
        self.transactions_manager = transactions_manager
        self.queue = transactions_manager.get_upload_queue()
        self.stop_flag = threading.Event()

    def stop(self):
        self.queue.put(QUIT_JOB)
        self.stop_flag.set()

    def run(self):
        while True:
            job = self.queue.get()
            transaction = None
            data_block = None
            key = None
            try:
                if job == QUIT_JOB or self.stop_flag.is_set():
                    break
                
                transaction, seek = job
                data_block,_,_ = transaction.get_data_block(seek)

                if not data_block.exists():
                    raise Exception('Data block %s does not found at local cache!'%data_block.get_name())

                try:
                    key = self.fabnet_gateway.put(data_block, replica_count=transaction.get_replica_count(), \
                            allow_rewrite=False)
                except Exception, err:
                    logger.error('Put data block error: %s'%err)
                    logger.error('Cant put data block from file %s. Wait %s seconds and try again...'%\
                            (transaction.get_file_path(), FG_ERROR_TIMEOUT))
                    time.sleep(FG_ERROR_TIMEOUT)
                    data_block.reopen()
                    self.queue.put(job)
                    continue

                data_block.close()
                self.transactions_manager.update_transaction(transaction.get_id(), seek, is_failed=False, foreign_name=key)
            except Exception, err:
                events_provider.critical('PutWorker', '%s failed: %s'%(transaction, err))
                logger.traceback_debug()            
                try:
                    if transaction:
                        self.transactions_manager.update_transaction(transaction.get_id(), seek, \
                                    is_failed=True)

                except Exception, err:
                    logger.error('[PutWorker.__on_error] %s'%err)
                    logger.traceback_debug()            
            finally:
                if data_block:
                    data_block.close()
                self.queue.task_done()


class GetWorker(threading.Thread):
    def __init__(self, fabnet_gateway, transactions_manager):
        threading.Thread.__init__(self)
        self.fabnet_gateway = fabnet_gateway
        self.transactions_manager = transactions_manager
        self.queue = transactions_manager.get_download_queue()
        self.stop_flag = threading.Event()

    def stop(self):
        self.queue.put(QUIT_JOB)
        self.stop_flag.set()

    def run(self):
        while True:
            out_streem = data = None
            job = self.queue.get()
            data_block = None
            transaction = None
            seek = None
            try:
                if job == QUIT_JOB or self.stop_flag.is_set():
                    break

                transaction, seek = job

                data_block,_,foreign_name = transaction.get_data_block(seek, noclone=False)
                if not foreign_name:
                    raise Exception('foreign name does not found for seek=%s'%seek)

                if transaction.is_failed():
                    logger.debug('Transaction {%s} is failed! Skipping data block downloading...'%transaction.get_id())
                    data_block.remove()
                    continue

                self.fabnet_gateway.get(foreign_name, transaction.get_replica_count(), data_block)
                data_block.close()

                self.transactions_manager.update_transaction(transaction.get_id(), seek, \
                            is_failed=False, foreign_name=data_block.get_name())
            except Exception, err:
                events_provider.error('GetWorker','%s failed: %s'%(transaction, err))
                logger.traceback_debug()            
                try:
                    if transaction and data_block:
                        self.transactions_manager.update_transaction(transaction.get_id(), seek, \
                                    is_failed=True, foreign_name=data_block.get_name())

                        data_block.remove()
                except Exception, err:
                    logger.error('[GetWorker.__on_error] %s'%err)
                    logger.traceback_debug()            
            finally:
                self.queue.task_done()


class DeleteWorker(threading.Thread):
    def __init__(self, fabnet_gateway, transactions_manager):
        threading.Thread.__init__(self)
        self.fabnet_gateway = fabnet_gateway
        self.queue = transactions_manager.get_delete_queue()
        self.stop_flag = threading.Event()

    def stop(self):
        self.queue.put(QUIT_JOB)
        self.stop_flag.set()

    def run(self):
        while True:
            job = self.queue.get()
            try:
                if job == QUIT_JOB or self.stop_flag.is_set():
                    break

                db_key, replica_count = job

                self.fabnet_gateway.remove(db_key, replica_count)
            except Exception, err:
                logger.error('DeleteWorker error: %s'%err)
                logger.traceback_debug()            
            finally:
                self.queue.task_done()


class WorkersManager:
    def __init__(self, worker_class, fabnet_gateway, transactions_manager, workers_count):
        self.__workers = []

        for i in xrange(workers_count):
            worker = worker_class(fabnet_gateway, transactions_manager)
            worker.setName('%s#%i'%(worker_class.__name__, i))
            self.__workers.append(worker)

    def start(self):
        for worker in self.__workers:
            worker.start()

    def stop(self):
        for worker in self.__workers:
            worker.stop()

        for worker in self.__workers:
            if worker.is_alive():
                worker.join()

