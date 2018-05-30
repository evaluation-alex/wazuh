#!/usr/bin/env python

# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is a free software; you can redistribute it and/or modify it under the terms of GPLv2

import logging
import json
import threading
import time
import os
import shutil
import ast
from operator import itemgetter
import errno
import fnmatch

from wazuh.cluster.cluster import get_cluster_items, _update_file, compress_files, decompress_files, get_files_status, get_cluster_items_client_intervals, unmerge_agent_info, merge_agent_info
from wazuh import common
from wazuh.utils import mkdir_with_mode
from wazuh.cluster.communication import ClientHandler, ProcessFiles, ClusterThread
from wazuh.cluster.internal_socket import InternalSocketHandler
from wazuh.cluster.dapi import dapi

logger = logging.getLogger(__name__)

#
# Client Handler
# There is only one ClientManagerHandler: the connection with master.
#
class ClientManagerHandler(ClientHandler):

    def __init__(self, cluster_config):
        ClientHandler.__init__(self, cluster_config['key'], cluster_config['nodes'][0], cluster_config['port'], cluster_config['node_name'])

        self.config = cluster_config
        self.integrity_received_and_processed = threading.Event()
        self.integrity_received_and_processed.clear()  # False

    # Overridden methods
    def handle_connect(self):
        ClientHandler.handle_connect(self)
        dir_path = "{}/queue/cluster/{}".format(common.ossec_path, self.name)
        if not os.path.exists(dir_path):
            mkdir_with_mode(dir_path)


    def process_request(self, command, data):
        logger.debug("[Client] [Request-R  ]: '{0}'.".format(command))

        if command == 'echo-m':
            return 'ok-m ', data.decode()
        elif command == 'sync_m_c':
            cmf_thread = ClientProcessMasterFiles(manager_handler=self, filename=data, stopper=self.stopper)
            cmf_thread.start()
            return 'ack', self.set_worker(command, cmf_thread, data)
        elif command == 'sync_m_c_ok':
            logger.info("[Client] [Integrity    ]: The master has verified that the integrity is right.")
            self.integrity_received_and_processed.set()
            return 'ack', "Thanks2!"
        elif command == 'sync_m_c_err':
            logger.info("[Client] [Integrity    ]: The master was not able to verify the integrity.")
            self.integrity_received_and_processed.set()
            return 'ack', "Thanks!"
        elif command == 'file_status':
            master_files = get_files_status('master', get_md5=True)
            client_files = get_files_status('client', get_md5=True)
            files = master_files
            files.update(client_files)
            return 'json', json.dumps(files)
        elif command == 'dapi':
            response = dapi.distribute_function(json.loads(data))
            return ['ok', response]
        else:
            return ClientHandler.process_request(self, command, data)


    def process_response(self, response):
        # FixMe: Move this line to communications
        answer, payload = self.split_data(response)
        logger.debug("[Client] [Response-R ]: '{0}'.".format(answer))

        if answer == 'ok-c':  # test
            response_data = '[response_only_for_client] Master answered: {}.'.format(payload)
        else:
            response_data = ClientHandler.process_response(self, response)

        return response_data

    # Private methods
    def _update_master_files_in_client(self, wrong_files, zip_path_dir, tag=None):
        def overwrite_or_create_files(filename, data, content=None):
            # Cluster items information: write mode and umask
            cluster_item_key = data['cluster_item_key']
            w_mode = cluster_items[cluster_item_key]['write_mode']
            umask = cluster_items[cluster_item_key]['umask']

            if content is None:
                # Full path
                file_path = common.ossec_path + filename
                zip_path = "{}/{}".format(zip_path_dir, filename)
                # File content and time
                with open(zip_path, 'r') as f:
                    file_data = f.read()
            else:
                file_data = content

            tmp_path='/queue/cluster/tmp_files'

            _update_file(file_path=filename, new_content=file_data,
                         umask_int=umask, w_mode=w_mode, tmp_dir=tmp_path, whoami='client')

        if not tag:
            tag = "[Client] [Sync process]"

        cluster_items = get_cluster_items()['files']

        before = time.time()
        error_shared_files = 0
        if wrong_files['shared']:
            logger.debug("{0}: Received {1} wrong files to fix from master. Action: Overwrite files.".format(tag, len(wrong_files['shared'])))
            for file_to_overwrite, data in wrong_files['shared'].items():
                try:
                    logger.debug2("{0}: Overwrite file: '{1}'".format(tag, file_to_overwrite))
                    if data['merged']:
                        for name, content, _ in unmerge_agent_info('agent-groups', zip_path_dir, file_to_overwrite):
                            overwrite_or_create_files(name, data, content)
                            if self.stopper.is_set():
                                break
                    else:
                        overwrite_or_create_files(file_to_overwrite, data)
                        if self.stopper.is_set():
                            break
                except Exception as e:
                    error_shared_files += 1
                    logger.debug2("{}: Error overwriting file '{}': {}".format(tag, file_to_overwrite, str(e)))
                    continue

        error_missing_files = 0
        if wrong_files['missing']:
            logger.debug("{0}: Received {1} missing files from master. Action: Create files.".format(tag, len(wrong_files['missing'])))
            for file_to_create, data in wrong_files['missing'].items():
                try:
                    logger.debug2("{0}: Create file: '{1}'".format(tag, file_to_create))
                    if data['merged']:
                        for name, content, _ in unmerge_agent_info('agent-groups', zip_path_dir, file_to_create):
                            overwrite_or_create_files(name, data, content)
                            if self.stopper.is_set():
                                break
                    else:
                        overwrite_or_create_files(file_to_create, data)
                        if self.stopper.is_set():
                            break
                except Exception as e:
                    error_missing_files += 1
                    logger.debug2("{}: Error creating file '{}': {}".format(tag, file_to_create, str(e)))
                    continue

        error_extra_files = 0
        if wrong_files['extra']:
            logger.debug("{0}: Received {1} extra files from master. Action: Remove files.".format(tag, len(wrong_files['extra'])))
            for file_to_remove in wrong_files['extra']:
                try:
                    logger.debug2("{0}: Remove file: '{1}'".format(tag, file_to_remove))
                    file_path = common.ossec_path + file_to_remove
                    try:
                        os.remove(file_path)
                    except OSError as e:
                        if e.errno == errno.ENOENT and '/queue/agent-groups/' in file_path:
                            logger.debug2("{}: File {} doesn't exist.".format(tag, file_to_remove))
                            continue
                        else:
                            raise e
                except Exception as e:
                    error_extra_files += 1
                    logger.debug2("{}: Error removing file '{}': {}".format(tag, file_to_remove, str(e)))
                    continue

                if self.stopper.is_set():
                    break

            directories_to_check = {os.path.dirname(f): cluster_items[data\
                                    ['cluster_item_key']]['remove_subdirs_if_empty']
                                    for f, data in wrong_files['extra'].items()}
            for directory in map(itemgetter(0), filter(lambda x: x[1], directories_to_check.items())):
                try:
                    full_path = common.ossec_path + directory
                    dir_files = set(os.listdir(full_path))
                    if not dir_files or dir_files.issubset(set(cluster_items['excluded_files'])):
                        shutil.rmtree(full_path)
                except Exception as e:
                    error_extra_files += 1
                    logger.debug2("{}: Error removing directory '{}': {}".format(tag, directory, str(e)))
                    continue

                if self.stopper.is_set():
                    break

        if error_extra_files or error_shared_files or error_missing_files:
            logger.error("{}: Found errors: {} overwriting, {} creating and {} removing".format(tag,
                        error_shared_files, error_missing_files, error_extra_files))

        after = time.time()
        logger.debug2("{}: Time updating integrity from master: {}s".format(tag, after - before))

        return True


    # New methods
    def send_integrity_to_master(self, reason=None, tag=None):
        if not tag:
            tag = "[Client] [Integrity]"

        logger.info("{0}: Reason: '{1}'".format(tag, reason))

        master_node = self.config['nodes'][0]  # Now, we only have 1 node: the master

        logger.info("{0}: Master found: {1}.".format(tag, master_node))

        logger.info("{0}: Gathering files.".format(tag))

        master_files = get_files_status('master')
        cluster_control_json = {'master_files': master_files, 'client_files': None}

        logger.info("{0}: Gathered files: {1}.".format(tag, len(cluster_control_json['master_files'])))

        logger.debug("{0}: Compressing files.".format(tag))
        # Compress data: control json
        compressed_data_path = compress_files(self.name, None, cluster_control_json)

        logger.debug("{0}: Files compressed.".format(tag))

        return compressed_data_path


    def send_client_files_to_master(self, reason=None, tag=None):
        data_for_master = None

        if not tag:
            tag = "[Client] [AgentInfo]"

        logger.info("{0}: Start. Reason: '{1}'".format(tag, reason))


        master_node = self.config['nodes'][0]  # Now, we only have 1 node: the master

        logger.info("{0}: Master found: {1}.".format(tag, master_node))


        logger.info("{0}: Gathering files.".format(tag))

        client_files = get_files_status('client', get_md5=False)
        cluster_control_json = {'master_files': {}, 'client_files': client_files}

        # Getting client file paths: agent-info, agent-groups.
        client_files_paths = client_files.keys()

        logger.debug("{0}: Files gathered: {1}.".format(tag, len(client_files_paths)))

        if len(client_files_paths) != 0:
            logger.info("{0}: There are agent-info files to send.".format(tag))

            # Compress data: client files + control json
            compressed_data_path = compress_files(self.name, client_files_paths, cluster_control_json)

            data_for_master = compressed_data_path

        else:
            logger.info("{0}: There are no agent-info files to send.".format(tag))

        return data_for_master


    def send_extra_valid_files_to_master(self, files, reason=None, tag=None):
        if not tag:
            tag = "[Client] [ReqFiles   ]"

        logger.info("{}: Start. Reason: '{}'.".format(tag, reason))

        master_node = self.config['nodes'][0]  # Now, we only have 1 node: the master

        logger.info("{0}: Master found: {1}.".format(tag, master_node))

        agent_groups_to_merge = set(fnmatch.filter(files.keys(), '*/agent-groups/*'))
        if agent_groups_to_merge:
            n_files, merged_file = merge_agent_info(merge_type='agent-groups',
                                              files=agent_groups_to_merge,
                                              time_limit_seconds=0)
            for ag in agent_groups_to_merge:
                del files[ag]

            if n_files:
                files.update({merged_file: {'merged': True,
                                            'merge_name': merged_file,
                                            'merge_type': 'agent-groups',
                                            'cluster_item_key': '/queue/agent-groups/'}})

        compressed_data_path = compress_files(self.name, files, {'client_files': files})

        return compressed_data_path


    def process_files_from_master(self, data_received, tag=None):

        if not tag:
            tag = "[Client] [process_files_from_master]"


        logger.info("{0}: Analyzing received files: Start.".format(tag))

        try:
            ko_files, zip_path  = decompress_files(data_received)
        except Exception as e:
            logger.error("{}: Error decompressing files from master: {}".format(tag, str(e)))
            raise e

        if ko_files:
            logger.info("{0}: Analyzing received files: Missing: {1}. Shared: {2}. Extra: {3}. ExtraValid: {4}".format(tag, len(ko_files['missing']), len(ko_files['shared']), len(ko_files['extra']), len(ko_files['extra_valid'])))
            logger.debug2("{0}: Received cluster_control.json: {1}".format(tag, ko_files))
        else:
            raise Exception("cluster_control.json not included in received zip file.")

        logger.info("{0}: Analyzing received files: End.".format(tag))

        # Update files
        if ko_files['extra_valid']:
            logger.info("{0}: Master requires some client files. Sending.".format(tag))
            if not "SyncExtraValidFilesThread" in set(map(lambda x: type(x).__name__, threading.enumerate())):
                req_files_thread = SyncExtraValidFilesThread(self, self.stopper, ko_files['extra_valid'])
                req_files_thread.start()
            else:
                logger.warning("{}: The last master's file request is in progress. Rejecting this request.".format(tag))

        if not ko_files['shared'] and not ko_files['missing'] and not ko_files['extra']:
            logger.info("{0}: Client meets integrity checks. No actions.".format(tag))
            sync_result = True
        else:
            logger.info("{0}: Client does not meet integrity checks. Actions required.".format(tag))

            logger.info("{0}: Updating files: Start.".format(tag))
            sync_result = self._update_master_files_in_client(ko_files, zip_path, tag)
            logger.info("{0}: Updating files: End.".format(tag))

        # remove temporal zip file directory
        shutil.rmtree(zip_path)

        return sync_result


#
# Threads (workers) created by ClientManagerHandler
#
class ClientProcessMasterFiles(ProcessFiles):

    def __init__(self, manager_handler, filename, stopper):
        ProcessFiles.__init__(self, manager_handler, filename, manager_handler.name, stopper)
        self.thread_tag = "[Client] [Integrity-R  ]"


    def check_connection(self):
        # if not self.manager_handler:
        #     self.sleep(2)
        #     return False

        if not self.manager_handler.is_connected():
            logger.info("{0}: Client is not connected. Waiting {1}s".format(self.thread_tag, 2))
            self.sleep(2)
            return False

        return True


    def lock_status(self, status):
        # the client only needs to do the unlock
        # because the lock was performed in the Integrity thread
        if not status:
            self.manager_handler.integrity_received_and_processed.set()


    def process_file(self):
        return self.manager_handler.process_files_from_master(self.filename, self.thread_tag)


    def unlock_and_stop(self, reason, send_err_request=None):
        logger.info("{0}: Unlocking due to {1}.".format(self.thread_tag, reason))
        ProcessFiles.unlock_and_stop(self, reason, send_err_request)


#
# Client
#
class ClientManager:
    SYNC_I_T = "Sync_I_Thread"
    SYNC_AI_T = "Sync_AI_Thread"
    KA_T = "KeepAlive_Thread"

    def __init__(self, cluster_config):
        self.handler = ClientManagerHandler(cluster_config=cluster_config)
        self.cluster_config = cluster_config

        # Threads
        self.stopper = threading.Event()
        self.threads = {}
        self._initiate_client_threads()

    # Private methods
    def _initiate_client_threads(self):
        logger.debug("[Master] Creating threads.")
        # Sync integrity
        self.threads[ClientManager.SYNC_I_T] = SyncIntegrityThread(client_handler=self.handler, stopper=self.stopper)
        self.threads[ClientManager.SYNC_I_T].start()

        # Sync AgentInfo
        self.threads[ClientManager.SYNC_AI_T] = SyncAgentInfoThread(client_handler=self.handler, stopper=self.stopper)
        self.threads[ClientManager.SYNC_AI_T].start()

        # KA
        self.threads[ClientManager.KA_T] = KeepAliveThread(client_handler=self.handler, stopper=self.stopper)
        self.threads[ClientManager.KA_T].start()

    # New methods
    def exit(self):
        logger.debug("[Client] Cleaning threads. Start.")

        # Cleaning client threads
        logger.debug("[Client] Cleaning main threads")
        self.stopper.set()

        for thread in self.threads:
            logger.debug2("[Client] Cleaning threads '{0}'.".format(thread))

            try:
                self.threads[thread].join(timeout=2)
            except Exception as e:
                logger.error("[Client] Cleaning '{0}' thread. Error: '{1}'.".format(thread, str(e)))

            if self.threads[thread].isAlive():
                logger.warning("[Client] Cleaning '{0}' thread. Timeout.".format(thread))
            else:
                logger.debug2("[Client] Cleaning '{0}' thread. Terminated.".format(thread))

        # Cleaning handler threads
        logger.debug("[Client] Cleaning handler threads.")
        self.handler.exit()

        logger.debug("[Client] Cleaning threads. End.")


#
# Client threads
#
class ClientThread(ClusterThread):

    def __init__(self, client_handler, stopper):
        ClusterThread.__init__(self, stopper)
        self.client_handler = client_handler

        # Intervals
        self.init_interval = 30
        self.interval = self.init_interval # It's set in specific threads


    def run(self):

        while not self.stopper.is_set() and self.running:

            # Wait until client is set and connected
            if not self.client_handler or not self.client_handler.is_connected():
                logger.debug2("{0}: Client is not set or connected. Waiting: {1}s.".format(self.thread_tag, 2))
                self.sleep(2)
                continue

            logger.info("{0}: Start.".format(self.thread_tag))

            try:
                self.interval = self.init_interval
                self.ask_for_permission()

                result = self.job()

                if result:
                    logger.info("{0}: Result: Successfully.".format(self.thread_tag))
                    self.process_result()
                else:
                    logger.error("{0}: Result: Error".format(self.thread_tag))
                    self.clean()
            except Exception as e:
                logger.error("{0}: Unknown Error: '{1}'.".format(self.thread_tag, str(e)))
                self.clean()

            logger.info("{0}: End. Sleeping: {1}s.".format(self.thread_tag, self.interval))
            self.sleep(self.interval)


    def ask_for_permission(self):
        raise NotImplementedError


    def clean(self):
        raise NotImplementedError


    def job(self):
        raise NotImplementedError


    def process_result(self):
        raise NotImplementedError


class KeepAliveThread(ClientThread):

    def __init__(self, client_handler, stopper):
        ClientThread.__init__(self, client_handler, stopper)
        self.thread_tag = "[Client] [KeepAlive-S  ]"
        # Intervals
        self.init_interval = get_cluster_items_client_intervals()['keep_alive']
        self.interval = self.init_interval



    def ask_for_permission(self):
        pass


    def clean(self):
        pass


    def job(self):
        return self.client_handler.send_request('echo-c', 'Keep-alive from client!')


    def process_result(self):
        pass


class SyncClientThread(ClientThread):
    def __init__(self, client_handler, stopper):
        ClientThread.__init__(self, client_handler, stopper)

        #Intervals
        self.init_interval = get_cluster_items_client_intervals()['sync_files']
        self.interval = self.init_interval

        self.interval_ask_for_permission = get_cluster_items_client_intervals()['ask_for_permission']


    def ask_for_permission(self):
        wait_for_permission = True
        n_seconds = 0

        logger.info("{0}: Asking permission to sync.".format(self.thread_tag))
        waiting_count = 0
        while wait_for_permission and not self.stopper.is_set() and self.running:
            response = self.client_handler.send_request(self.request_type)
            processed_response = self.client_handler.process_response(response)

            if processed_response:
                if 'True' in processed_response:
                    logger.info("{0}: Permission granted.".format(self.thread_tag))
                    wait_for_permission = False

            sleeped = self.sleep(self.interval_ask_for_permission)
            n_seconds += sleeped
            if n_seconds >= 5 and n_seconds % 5 == 0:
                waiting_count += 1
                logger.info("{0}: Waiting for Master permission to sync [{1}].".format(self.thread_tag, waiting_count))


    def clean(self):
        pass


    def job(self):
        sync_result = True
        compressed_data_path = self.function(reason="Interval", tag=self.thread_tag)

        if compressed_data_path:
            logger.info("{0}: Sending files to master.".format(self.thread_tag))

            response = self.client_handler.send_file(reason = self.reason, file_to_send= compressed_data_path, remove = True)

            processed_response = self.client_handler.process_response(response)
            if processed_response:
                logger.info("{0}: Sync accepted by the master.".format(self.thread_tag))
            else:
                sync_result = False
                logger.error("{0}: Sync error reported by the master.".format(self.thread_tag))

        return sync_result


    def process_result(self):
        pass


class SyncIntegrityThread(SyncClientThread):

    def __init__(self, client_handler, stopper):
        SyncClientThread.__init__(self, client_handler, stopper)
        self.init_interval = get_cluster_items_client_intervals()['sync_integrity']
        self.interval = self.init_interval

        self.request_type = "sync_i_c_m_p"
        self.reason = "sync_i_c_m"
        self.function = self.client_handler.send_integrity_to_master
        self.thread_tag = "[Client] [Integrity-S  ]"


    def job(self):
        # The client is going to send the integrity, so it is not received and processed
        self.client_handler.integrity_received_and_processed.clear()
        return SyncClientThread.job(self)


    def process_result(self):
        # The client sent the integrity.
        # It must wait until integrity_received_and_processed is set:
        #  - Master sends files: sync_m_c AND the client processes the integrity.
        #  - Master sends error: sync_m_c_err
        #  - Master sends error: sync_m_c_ok
        #  - Thread is stopped (all threads - stopper, just this thread - running)
        #  - Client is disconnected and connected again
        logger.info("{0}: Locking: Waiting for receiving Master response and process the integrity if necessary.".format(self.thread_tag))

        n_seconds = 0
        while not self.client_handler.integrity_received_and_processed.isSet() and not self.stopper.is_set() and self.running:
            event_is_set = self.client_handler.integrity_received_and_processed.wait(1)
            n_seconds += 1

            if event_is_set:  # No timeout -> Free
                logger.info("{0}: Unlocking: Master sent the response and the integrity was processed if necessary.".format(self.thread_tag))
                self.interval = max(0, self.init_interval - n_seconds)
            else:  # Timeout
                # Print each 5 seconds
                if n_seconds != 0 and n_seconds % 5 == 0:
                    logger.info("{0}: Master did not send the integrity in the last 5 seconds. Waiting.".format(self.thread_tag))


    def clean(self):
        SyncClientThread.clean(self)
        self.client_handler.integrity_received_and_processed.clear()


class SyncAgentInfoThread(SyncClientThread):

    def __init__(self, client_handler, stopper):
        SyncClientThread.__init__(self, client_handler, stopper)
        self.thread_tag = "[Client] [AgentInfo-S  ]"
        self.request_type = "sync_ai_c_mp"
        self.reason = "sync_ai_c_m"
        self.function = self.client_handler.send_client_files_to_master


class SyncExtraValidFilesThread(SyncClientThread):

    def __init__(self, client_handler, stopper, files):
        SyncClientThread.__init__(self, client_handler, stopper)
        self.thread_tag = "[Client] [AgentGroup-S ]"
        self.request_type = "sync_ev_c_mp"
        self.reason = "sync_ev_c_m"
        self.function = self.client_handler.send_extra_valid_files_to_master
        self.files = files

    def job(self):
        result = False
        compressed_data_path = self.function(reason="ExtraValid files", tag=self.thread_tag,
                                             files=self.files)

        logger.info("{0}: Sending files to master.".format(self.thread_tag))

        response = self.client_handler.send_file(reason = self.reason,
                                                 file_to_send= compressed_data_path, remove = True)

        processed_response = self.client_handler.process_response(response)
        if processed_response:
            logger.info("{0}: ExtraValid files accepted by the master.".format(self.thread_tag))
            result = True
        else:
            logger.error("{0}: ExtraValid files error reported by the master.".format(self.thread_tag))

        self.stop()
        return result


#
# Internal socket
#
class ClientInternalSocketHandler(InternalSocketHandler):
    def __init__(self, sock, manager, asyncore_map, addr):
        InternalSocketHandler.__init__(self, sock=sock, server=manager, asyncore_map=asyncore_map, addr=addr)

    def process_request(self, command, data):
        logger.debug("[Transport-I] Forwarding request to cluster clients '{0}' - '{1}'".format(command, data))

        if command not in ['get_nodes','get_health','dapi']:  # ToDo: create a list of valid internal socket commands
            response = InternalSocketHandler.process_request(self, command, data)
        else:
            node_response = self.server.manager.handler.send_request(command=command, data=data if data != 'None' else None).split(' ', 1)
            type_response = node_response[0]
            response = node_response[1]
            if type_response == "err":
                response = ["err", json.dumps({"err": response})]
            else:
                response = ['ok', response]

        return response
