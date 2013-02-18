#!/usr/bin/python
"""
Copyright (C) 2013 Konstantin Andrusenko
    See the documentation for further information on copyrights,
    or contact the author. All Rights Reserved.

@package nimbus_client.core.smart_file_object
@author Konstantin Andrusenko
@date February 17, 2013

This module contains the implementation of SmartFileObject class
"""
from nimbus_client.core.transactions_manager import TransactionsManager, Transaction
from nimbus_client.core.constants import MAX_DATA_BLOCK_SIZE

class SmartFileObject:
    TRANSACTIONS_MANAGER = None
    @classmethod
    def setup_transaction_manager(cls, transactions_manager):
        if not isinstance(transactions_manager, TransactionsManager):
            raise RuntimeError('Unknown type of transactions manager: %s'%type(transaction_manager))
        cls.TRANSACTIONS_MANAGER = transactions_manager

    def __init__(self, file_path):
        self.__file_path = file_path
        self.__seek = 0
        self.__cur_data_block = None
        self.__cur_db_seek = 0
        self.__transaction_id = None
        self.__unsync = False

    def write(self, data):
        try:
            if self.__cur_data_block is None:
                self.__cur_data_block = self.TRANSACTIONS_MANAGER.new_data_block()

            data_len = len(data)
            rest = self.__cur_db_seek + data_len - MAX_DATA_BLOCK_SIZE
            if rest > 0:
                rest_data = data[data_len-rest:]
                data = data[:data_len-rest]
            else:
                rest_data = ''

            self.__cur_data_block.write(data)
            self.__cur_db_seek += len(data)
            self.__unsync = True

            if rest_data:
                self.__send_data_block()
                self.write(rest_data)
                self.__cur_db_seek += len(rest_data)
        except Exception, err:
            self.__failed_transaction(err)
            raise err

    def read(self, read_len=None):
        if not self.__transaction_id:
            self.__transaction_id = self.TRANSACTIONS_MANAGER.start_transaction(Transaction.TT_READ, self.__file_path)

        ret_data = ''
        while True:
            if not self.__cur_data_block:
                if self.__seek is None:
                    break
                self.__cur_data_block, self.__seek = self.TRANSACTIONS_MANAGER.get_data_block(self.__transaction_id, self.__seek)

            data = self.__cur_data_block.read(read_len)
            if data:
                ret_data += data

            if read_len and len(ret_data) >= read_len:
                break
            self.__cur_data_block.close()
            self.__cur_data_block = None

        return ret_data


    def close(self):
        if self.__unsync:
            if self.__cur_data_block:
                self.__send_data_block()
            self.TRANSACTIONS_MANAGER.update_transaction_state(self.__transaction_id, Transaction.TS_LOCAL_SAVED)
            
    def __send_data_block(self):
        self.__cur_data_block.finalize()

        if not self.__transaction_id:
            self.__transaction_id = self.TRANSACTIONS_MANAGER.start_transaction(Transaction.TT_WRITE, self.__file_path)

        self.TRANSACTIONS_MANAGER.transfer_data_block(self.__transaction_id, self.__seek, self.__cur_db_seek, self.__cur_data_block)

        self.__seek += self.__cur_db_seek
        self.__cur_db_seek = 0
        self.__cur_data_block = None
        self.__unsync = False

    def __failed_transaction(self, err):
        #TODO: implement error writing to events log
        if self.__transaction_id:
            self.TRANSACTIONS_MANAGER.update_transaction_state(self.__transaction_id, Transaction.TS_FAILED)


