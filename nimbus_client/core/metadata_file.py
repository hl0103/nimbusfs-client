#!/usr/bin/python
"""
Copyright (C) 2013 Konstantin Andrusenko
    See the documentation for further information on copyrights,
    or contact the author. All Rights Reserved.

@package nimbus_client.core.metadata_file
@author Konstantin Andrusenko
@date January 20, 2013

This module contains the implementation of Metadata class
"""
import os
import glob
import anydbm

from nimbus_client.core.metadata import *
from nimbus_client.core.exceptions import NoFreeIdentificator
from nimbus_client.core.journal import Journal
from nimbus_client.core.base_safe_object import LockObject
from nimbus_client.core.logger import logger
from nimbus_client.core.utils import to_str

MDLock = LockObject()

RESERVE_ITEM = 'RI'
MAX_ITEM_ID = MAX_L 
ROOT_NAME = '/'

class Key:
    KEY_STRUCT = '<QiB'
    KEY_LEN = struct.calcsize(KEY_STRUCT)

    #key types
    KT_ADDR = 1
    KT_ITEM = 2

    @classmethod
    def from_dump(cls, dumped):
        parent_id, item_hash, key_type = struct.unpack(cls.KEY_STRUCT, dumped)
        return Key(key_type, parent_id, item_hash)

    def __init__(self, key_type, parent_id, item_hash=0):
        self.key_type = key_type
        self.parent_id = parent_id
        self.item_hash = item_hash

    def to_item_key(self):
        self.key_type = self.KT_ITEM
        self.item_hash = 0


    def __str__(self):
        return '%s_%08x_%08x'%('addr' if self.key_type==self.KT_ADDR else 'item', self.parent_id, self.item_hash)

    def dump(self):
        return struct.pack(self.KEY_STRUCT, self.parent_id, self.item_hash, self.key_type)


class ChildAddrList:
    HDR_STRUCT = '<IIQ'
    HDR_LEN = struct.calcsize(HDR_STRUCT)
    ADDR_STRUCT = '<Q'
    ADDR_LEN = struct.calcsize(ADDR_STRUCT)
    PADDING_SIZE = 256

    @classmethod
    def header_from_dump(cls, dumped):
        hdr = dumped[:cls.HDR_LEN]
        b_size, a_size, item_id = struct.unpack(cls.HDR_STRUCT, hdr)
        return item_id, dumped[b_size:]

    @classmethod
    def from_dump(cls, dumped):
        hdr = dumped[:cls.HDR_LEN]
        b_size, a_size, item_id = struct.unpack(cls.HDR_STRUCT, hdr)
        ch_addr_list = ChildAddrList(item_id)
        raw = dumped[cls.HDR_LEN:a_size]
        
        len_s = len(raw)
        cur_i = 0
        while True:
            if cur_i >= len_s:
                break
            buf = raw[cur_i: cur_i+cls.ADDR_LEN]
            i_id, = struct.unpack(cls.ADDR_STRUCT, buf)
            ch_addr_list.append_addr(i_id)
            cur_i += cls.ADDR_LEN

        return ch_addr_list, dumped[b_size:]

    def __init__(self, item_id):
        self.item_id = item_id
        self.child_ids = []

    def __iter__(self):
        for i_id in self.child_ids:
            yield i_id

    def __len__(self):
        return len(self.child_ids)

    def append_addr(self, i_id):
        self.child_ids.append(i_id)

    def remove(self, i_id):
        try:
            idx = self.child_ids.index(i_id)
        except ValueError, err:
            raise Exception('No item id %s found'%i_id)
        del self.child_ids[idx]

    def __str__(self):
        return '{%08x: %s}'%(self.item_id, ' '.join(['%08x'%c for c in self.child_ids]))

    def dump(self):
        a_size = len(self.child_ids) * self.ADDR_LEN + self.HDR_LEN
        b_size = ((a_size / self.PADDING_SIZE) + 1) * self.PADDING_SIZE
        pad_size = b_size - a_size
        hdr = struct.pack(self.HDR_STRUCT, b_size, a_size, self.item_id)
        raw = ''
        for i_id in self.child_ids:
            raw += struct.pack(self.ADDR_STRUCT, i_id)
        return ''.join([hdr, raw, ' '*pad_size])

        

class AddressItems:
    @classmethod
    def from_dump(cls, dumped):
        items = AddressItems()
        while True:
            ch_addr_list, dumped = ChildAddrList.from_dump(dumped)
            items.append(ch_addr_list)
            if not dumped:
                break
        return items

    @classmethod
    def iter_item_ids(cls, dumped):
        ids = []
        while True:
            i_id, dumped = ChildAddrList.header_from_dump(dumped)
            yield i_id
            if not dumped:
                break

    def __init__(self):
        self.__item_addrs = []

    def __iter__(self):
        for i_addr in self.__item_addrs:
            yield i_addr

    def __str__(self):
        return ' '.join([str(i) for i in self.__item_addrs])

    def dump(self):
        s = ''
        for i_addr in self.__item_addrs:
            s += i_addr.dump()
        return s

    def __len__(self):
        return len(self.__item_addrs)

    def append(self, item_addr):
        if not isinstance(item_addr, ChildAddrList):
            raise Exception('AddressItems can append only ChildAddrList objects')
        self.__item_addrs.append(item_addr)

    def remove(self, item_id):
        idx = None
        for i, item_addr in enumerate(self):
            if item_addr.item_id == item_id:
                idx = i
                break
        else:
            raise Exception('Item %s does not found'%item_id)
        del_item = self.__item_addrs[idx]
        del self.__item_addrs[idx]
        return del_item



class MetadataFile:
    ITEM_HDR_STRUCT = '<IIB'
    ITEM_HDR_SIZE = struct.calcsize(ITEM_HDR_STRUCT)
    ITEM_PADDING_SIZE = 128

    IT_DIRECTORY =  0x0f
    IT_FILE = 0x0e

    def __init__(self, md_file_path='md.cache', journal=None):
        self.__root_id = 0
        self.__journal = journal
        self.__valid = False
        self.__load_md_db(md_file_path)

    def __remove_md_file(self, file_path):
        for real_file_path in glob.glob('%s*'%file_path):
            os.remove(real_file_path)

    def __load_md_db(self, md_file_path):
        self.db =  anydbm.open(md_file_path, 'c')
        self.__last_item_id = long(self.__get_db_val('last_item_id', 0))
        self.__last_journal_rec_id = long(self.__get_db_val('last_journal_rec_id', 0))
        if self.__journal:
            j_key = self.__get_db_val('journal_key', None)
            if j_key != self.__journal.get_journal_key():
                logger.info('Invalid journal key in metadata database! Recreating it...')
                self.db.close()
                self.__remove_md_file(md_file_path)
                self.db = anydbm.open(md_file_path, 'c')
                self.db['journal_key'] = self.__journal.get_journal_key()
                self.__last_item_id = 0
                self.__last_journal_rec_id = 0

        try:
            self.__init_from_journal(self.__last_journal_rec_id)
        except NimbusException, err:
            logger.error('Metadata was not restored from journal! Details: %s'%err)

            logger.info('Trying restoring full journal records...')
            self.db.close()
            self.__remove_md_file(md_file_path)
            self.db = anydbm.open(md_file_path, 'c')
            self.__init_from_journal(0)

    def __get_db_val(self, key, default=None):
        if self.db.has_key(key):
            return self.db[key]
        return default

    def __init_from_journal(self, start_rec_id):
        if self.__journal:
            logger.info('Restoring journal from ID=%s ...'%start_rec_id)
            for record_id, operation_type, item_md in self.__journal.iter(start_rec_id):
                try:
                    if operation_type == Journal.OT_APPEND:
                        self.append(None, item_md)
                        self.__last_item_id = item_md.item_id
                    elif operation_type == Journal.OT_UPDATE:
                        self.update(item_md)
                    elif operation_type == Journal.OT_REMOVE:
                        try:
                            item_md = self.__get_item_md(item_md)
                            self.remove(item_md)
                        except NotFoundException, err:
                            logger.warning('Can not remove item with ID=%s, bcs it does not found!'%item_md)
                        except NotEmptyException, err:
                            logger.warning('Can not remove item with ID=%s, bcs it has children!'%item_md)
                except AlreadyExistsException, err:
                    logger.warning('Can not append/update item %s, bcs it is already exists!'%item_md)

            self.__last_journal_rec_id = self.__journal.get_last_id()
            logger.info('Metadata is restored from journal. Last journal record ID=%s'%self.__last_journal_rec_id)
        else:
            self.append(None, DirectoryMD(name=ROOT_NAME, item_id=0, parent_dir_id=0))
        self.__valid = True

    def __hash(self, str_data):
        return zlib.adler32(str_data)

    def __set_raw_value(self, key, value):
        self.db[key.dump()] = value

    def __get_raw_value(self, key, default=None):
        try:
            ret = self.db[key.dump()]
            return ret
        except KeyError, err:
            return default

    def __key_exists(self, key):
        return self.db.has_key(key.dump())

    def __remove_key(self, key):
        del self.db[key.dump()]

    def __get_child_id(self, dir_id, item_name):
        ikey = Key(Key.KT_ADDR, dir_id, self.__hash(item_name))
        i_ids = []
        ret_id = None
        i_addr_list_raw = self.__get_raw_value(ikey)
        if not i_addr_list_raw:
            raise PathException('key %s does not found'%ikey)

        for i_id in AddressItems.iter_item_ids(i_addr_list_raw):
            i_ids.append(i_id)

        if len(i_ids) == 0:
            raise PathException('No childlen found for key %s'%ikey)

        if len(i_ids) > 1:
            for i_id in i_ids:
                item_md = self.__get_item_md(i_id)
                if to_str(item_md.name) == to_str(item_name):
                    ret_id = i_id
                    break
        else:
            ret_id = i_ids[0]

        if ret_id is None:
            raise PathException('No child "%s" found in dir with id %s'%(item_name, dir_id))

        return ret_id

    def __get_item_md(self, item_id):
        ikey = Key(Key.KT_ITEM, item_id)
        raw_item = self.__get_raw_value(ikey)
        if raw_item is None:
            raise NotFoundException('Item metadata no found for key %s'%ikey)
        raw_item, item_type = self.__get_item_raw_padding(raw_item)
        item = None
        if item_type == self.IT_DIRECTORY:
            item = DirectoryMD(dumped_md=raw_item)
        elif item_type == self.IT_FILE:
            item = FileMD(dumped_md=raw_item)
        else:
            raise Exception('Unknown item type: %s'%item_type)

        item.item_id = item_id
        return item

    def __do_item_raw_padding(self, item_md):
        if item_md.is_file():
            i_type = self.IT_FILE
        else:
            i_type = self.IT_DIRECTORY

        i_dump = item_md.dump()
        i_size = len(i_dump) + self.ITEM_HDR_SIZE
        b_size = ((i_size / self.ITEM_PADDING_SIZE) + 1) * self.ITEM_PADDING_SIZE

        pad_size = b_size - i_size
        hdr = struct.pack(self.ITEM_HDR_STRUCT, b_size, i_size, i_type)
        return ''.join([hdr, i_dump, ' '*pad_size])

    def __get_item_raw_padding(self, raw_item_md):
        hdr = raw_item_md[:self.ITEM_HDR_SIZE]
        b_size, i_size, i_type = struct.unpack(self.ITEM_HDR_STRUCT, hdr)
        return raw_item_md[self.ITEM_HDR_SIZE:i_size], i_type

    def __update_addr_item(self, old_item_md, new_item_md):
        if to_str(old_item_md.name) != to_str(new_item_md.name):
            new_key = Key(Key.KT_ADDR, new_item_md.parent_dir_id, self.__hash(new_item_md.name))
            old_key = Key(Key.KT_ADDR, old_item_md.parent_dir_id, self.__hash(old_item_md.name))
            self.__update_addr(old_key, old_item_md.item_id, new_key)

        if old_item_md.parent_dir_id != new_item_md.parent_dir_id:
            old_dir_md = self.__get_item_md(old_item_md.parent_dir_id)
            self.__remove_addr_child(old_dir_md, old_item_md.item_id)

            new_dir_md = self.__get_item_md(new_item_md.parent_dir_id)
            self.__append_addr_child(new_dir_md, old_item_md.item_id)

    def __update_addr(self, a_key, item_id, save_a_key=None):
        if not save_a_key:
            save_a_key = a_key

        addr_st_raw = self.__get_raw_value(a_key)
        if addr_st_raw:
            addr_items = AddressItems.from_dump(addr_st_raw)
        else:
            addr_items = AddressItems()

        if a_key != save_a_key:
            addr_item = addr_items.remove(item_id)
            if len(addr_items) == 0:
                self.__remove_key(a_key)

            addr_st_raw = self.__get_raw_value(save_a_key)
            if addr_st_raw:
                addr_items = AddressItems.from_dump(addr_st_raw)
            else:
                addr_items = AddressItems()
        else:
            addr_item = ChildAddrList(item_id)

        addr_items.append(addr_item)
        self.__set_raw_value(save_a_key, addr_items.dump())

    def __append_addr_child(self, dir_md, item_id):
        par_a_key = Key(Key.KT_ADDR, dir_md.parent_dir_id, self.__hash(dir_md.name))
        addr_st_raw = self.__get_raw_value(par_a_key)
        if addr_st_raw:
            addr_items = AddressItems.from_dump(addr_st_raw)
            for addr_item in addr_items:
                if addr_item.item_id == dir_md.item_id:
                    addr_item.append_addr(item_id)
                    break
        else:
            addr_items = AddressItems()
            addr_item = ChildAddrList(dir_md.item_id)
            addr_item.append_addr(item_id)
            addr_items.append(addr_item)
        
        self.__set_raw_value(par_a_key, addr_items.dump())

    def __remove_addr_child(self, dir_md, item_id):
        par_a_key = Key(Key.KT_ADDR, dir_md.parent_dir_id, self.__hash(dir_md.name))
        addr_st_raw = self.__get_raw_value(par_a_key)
        if not addr_st_raw:
            raise Exception('No address record found for %s'%dir_md)
        addr_items = AddressItems.from_dump(addr_st_raw)
        for addr_item in addr_items:
            if addr_item.item_id == dir_md.item_id:
                addr_item.remove(item_id)
        self.__set_raw_value(par_a_key, addr_items.dump())
        
    def __exists(self, item_md):
        a_key = Key(Key.KT_ADDR, item_md.parent_dir_id, self.__hash(item_md.name))
        addr_st_raw = self.__get_raw_value(a_key)
        if not addr_st_raw:
            return False

        for i_id in AddressItems.iter_item_ids(addr_st_raw):
            i_md = self.__get_item_md(i_id)
            if to_str(i_md.name) == to_str(item_md.name):
                return True

        return False

    def __update_journal(self, op_type, item_md):
        if self.__journal and self.__valid:
            if item_md.is_local:
                return
            self.__last_journal_rec_id = self.__journal.append(op_type, item_md)

    @MDLock
    def listdir(self, path):
        dir_md = self.find(path)
        par_a_key = Key(Key.KT_ADDR, dir_md.parent_dir_id, self.__hash(dir_md.name))
        addr_st_raw = self.__get_raw_value(par_a_key)
        ret_lst = [] 
        if addr_st_raw:
            addr_items = AddressItems.from_dump(addr_st_raw)
            for addr_item in addr_items:
                if addr_item.item_id == dir_md.item_id:
                    for i_id in addr_item:
                        ret_lst.append(self.__get_item_md(i_id))
        return ret_lst

    def __get_next_item_id(self, with_reserve=False):
        self.__last_item_id
        cur_item_id = self.__last_item_id
        while True:
            self.__last_item_id += 1
            if self.__last_item_id >= MAX_ITEM_ID:
                self.__last_item_id = 1
            if cur_item_id == self.__last_item_id:
                raise NoFreeIdentificator('No free ItemMD identificator found!')
            if self.__register_item_by_id(self.__last_item_id, not with_reserve):
                return self.__last_item_id

    def __register_item_by_id(self, item_id, check_only=False):
        ikey = Key(Key.KT_ITEM, item_id)
        raw_item = self.__get_raw_value(ikey)
        if raw_item is None:
            if not check_only:
                self.__set_raw_value(ikey, RESERVE_ITEM)
            return True
        return False 

    @MDLock
    def generate_item_id(self):
        return self.__get_next_item_id(with_reserve=True)

    @MDLock
    def cancel_item_id_reserve(self, item_id):
        ikey = Key(Key.KT_ITEM, item_id)
        raw_item = self.__get_raw_value(ikey)
        if raw_item == RESERVE_ITEM:
            self.__remove_key(ikey) 

    @MDLock
    def append(self, path, item_md, item_id=None):
        if path:
            if not item_id:
                item_id = self.__get_next_item_id()
            dir_md = self.find(path)
            item_md.item_id = item_id
            item_md.parent_dir_id = dir_md.item_id
        else:
            if item_md.parent_dir_id is None:
                raise Exception('Parent ID does not found for item {%s}'%item_md)

            if item_md.item_id == 0:
                #append root dir to fs
                i_key = Key(Key.KT_ITEM, 0)
                self.__set_raw_value(i_key, self.__do_item_raw_padding(item_md))
                return

            dir_md = self.__get_item_md(item_md.parent_dir_id)

        i_key = Key(Key.KT_ITEM, item_md.item_id)
        a_key = Key(Key.KT_ADDR, dir_md.item_id, self.__hash(item_md.name))
        par_a_key = Key(Key.KT_ADDR, dir_md.parent_dir_id, self.__hash(dir_md.name))

        if self.__exists(item_md):
            raise AlreadyExistsException('Item "%s" alredy exists in %s'%(item_md.name, path))
        iv = self.__get_raw_value(i_key)
        if iv and iv != RESERVE_ITEM:
            raise AlreadyExistsException('Item with ID=%s is already exists!'%item_md.item_id)

        self.__append_addr_child(dir_md, item_md.item_id)
        self.__update_addr(a_key, item_md.item_id)
        self.__set_raw_value(i_key, self.__do_item_raw_padding(item_md))

        if dir_md.item_id > 0:
            i_key = Key(Key.KT_ITEM, dir_md.item_id)
            dir_md.update_datetime()
            self.__set_raw_value(i_key, self.__do_item_raw_padding(dir_md))

        self.__update_journal(Journal.OT_APPEND, item_md)

    @MDLock
    def update(self, item_md):
        if item_md.item_id is None:
            raise Exception('Item ID does not found for item {%s}'%item_md)

        old_md = self.__get_item_md(item_md.item_id)
        self.__update_addr_item(old_md, item_md)
        i_key = Key(Key.KT_ITEM, item_md.item_id)
        if item_md.is_dir():
            item_md.update_datetime()
        self.__set_raw_value(i_key, self.__do_item_raw_padding(item_md))

        self.__update_journal(Journal.OT_UPDATE, item_md)

    @MDLock
    def remove(self, item_md):
        if item_md.item_id is None:
            raise Exception('Item ID does not found for item {%s}'%item_md)
        if not self.__exists(item_md):
            raise NoMetadataException('Item "%s" does not exists in dir with id %s'%(item_md.name, item_md.parent_dir_id))

        i_key = Key(Key.KT_ITEM, item_md.item_id)
        a_key = Key(Key.KT_ADDR, item_md.parent_dir_id, self.__hash(item_md.name))
        if item_md.is_dir():
            #check item children items...
            addr_st_raw = self.__get_raw_value(a_key)
            if addr_st_raw:
                addr_items = AddressItems.from_dump(addr_st_raw)
                for addr_item in addr_items:
                    if addr_item.item_id == item_md.item_id:
                        if len(addr_item):
                            raise NotEmptyException('Item "%s" has children items!'%item_md)
                        break

        #remove item from parent directory
        dir_md = self.__get_item_md(item_md.parent_dir_id)
        self.__remove_addr_child(dir_md, item_md.item_id)

        #remove address struct
        self.__remove_key(a_key)

        #remove item metadata
        self.__remove_key(i_key)

        self.__update_journal(Journal.OT_REMOVE, item_md)

    @MDLock
    def find(self, path):
        items = path.split('/')
        cur_id = self.__root_id
        try:
            for item_name in items:
                if (not item_name) or (item_name == ROOT_NAME):
                    continue

                cur_id = self.__get_child_id(cur_id, item_name)
            return self.__get_item_md(cur_id)
        except PathException, err:
            raise PathException('Path %s does not found (Internal: %s)'%(path, err))

    def exists(self, path):
        try:
            self.find(path)
            return True
        except PathException, err:
            return False

    @MDLock
    def close(self):
        if not self.db:
            return
        self.db['last_journal_rec_id'] = str(self.__last_journal_rec_id)
        self.db['last_item_id'] = str(self.__last_item_id)
        self.db.close()
        self.db = None

    @MDLock
    def _print(self):
        addr_keys = []
        item_keys = []
        for key in self.db.keys():
            k = Key.from_dump(key)
            if k.key_type == Key.KT_ADDR:
                addr_keys.append(k)
            else:
                item_keys.append(k)

        print '....ADDRESSES TABLE....'
        for a_key in addr_keys:
            addr_st_raw = self.__get_raw_value(a_key)
            a_items = AddressItems.from_dump(addr_st_raw)
            print '[%s] %s'%(a_key, a_items)

        print '...ITEMS TABLE...'
        for i_key in item_keys:
            md = self.__get_item_md(i_key.parent_id)
            print '[%s] %s'%(i_key, md)
        print '----------------------'




