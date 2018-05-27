"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""
import time
from resource_management.core.logger import Logger
from resource_management.core.resources.system import Directory, Execute, File
from resource_management.libraries.functions.format import format
from resource_management.libraries.resources.properties_file import PropertiesFile


def backup_collection(env):
  """
  Backup collections using replication API (as Solr Cloud Backup API is not available in Solr 5)
  If the cluster is not kerberized, it will be needed to resolve ip addresses to hostnames (as SOLR_HOST=`hostname -f` is not used by default in infra-solr-env)
  """
  import params, command_commons
  env.set_params(command_commons)

  Directory(command_commons.index_location,
            mode=0755,
            cd_access='a',
            create_parents=True,
            owner=params.infra_solr_user,
            group=params.user_group
            )
  host_cores_data_map = command_commons.get_host_cores_for_collection()

  Logger.info(format("Backup Solr Collection {collection} to {index_location}"))

  host_core_map = host_cores_data_map[command_commons.HOST_CORES]

  host_or_ip = params.hostname
  # IP resolve - for unsecure cluster
  host_ip_pairs = {}
  if not params.security_enabled:
    keys = host_core_map.keys()
    for key in keys:
      if command_commons.is_ip(key):
        resolved_hostname = command_commons.resolve_ip_to_hostname(key)
        host_ip_pairs[resolved_hostname] = key

  if params.hostname in host_ip_pairs:
    host_or_ip = host_ip_pairs[params.hostname]

  cores = host_core_map[host_or_ip] if host_or_ip in host_core_map else []

  for core in cores:
    if core in command_commons.skip_cores:
      Logger.info(format("Core '{core}' is filtered out."))
      continue
    solr_request_path = format("{core}/replication?command=BACKUP&location={index_location}&name={core}&wt=json")
    backup_api_cmd = command_commons.create_solr_api_request_command(solr_request_path)

    Execute(backup_api_cmd, user=params.infra_solr_user, logoutput=True)

    if command_commons.request_async is False:
      Logger.info("Sleep 5 seconds to wait until the backup request is executed.")
      time.sleep(5)
      Logger.info("Check backup status ...")
      solr_status_request_path = format("{core}/replication?command=details&wt=json")
      status_check_json_output = format("{index_location}/backup_status.json")
      status_check_cmd = command_commons.create_solr_api_request_command(solr_status_request_path,
                                                                         status_check_json_output)
      command_commons.snapshot_status_check(status_check_cmd, status_check_json_output, core, True,
                                            log_output=command_commons.log_output, tries=command_commons.request_tries,
                                            time_interval=command_commons.request_time_interval)


def restore_collection(env):
  """
  Restore collections - by copying snapshots with backup_* prefix, then remove old one and remove backup_* prefixes from the folder names.
  """
  import params, command_commons
  env.set_params(command_commons)

  if command_commons.solr_num_shards == 0:
    raise Exception(format("The 'solr_shards' command parameter is required to set."))

  host_cores_backup_map = command_commons.read_backup_json()
  host_cores_map = command_commons.get_host_cores_for_collection(backup=False)

  original_core_host_pairs = command_commons.sort_core_host_pairs(host_cores_backup_map[command_commons.CORE_HOST])
  new_core_host_pairs = command_commons.sort_core_host_pairs(host_cores_map[command_commons.CORE_HOST])

  core_pairs = command_commons.create_core_pairs(original_core_host_pairs, new_core_host_pairs)
  Logger.info("Generated core pairs: " + str(core_pairs))

  Logger.info(format("Remove write.lock files from folder '{index_location}'"))
  for write_lock_file in command_commons.get_files_by_pattern(format("{index_location}"), 'write.lock'):
    File(write_lock_file, action="delete")

  Logger.info(format("Restore Solr Collection {collection} from {index_location} ..."))

  if command_commons.collection in ["ranger_audits", "history", "hadoop_logs", "audit_logs",
                                    "vertex_index", "edge_index",
                                    "fulltext_index"]:  # Make sure ambari wont delete an important collection
    raise Exception(format(
      "Selected collection for restore is: {collection}. It is not recommended to restore on default collections."))

  hdfs_cores_on_host=[]

  for core_data in core_pairs:
    src_core = core_data['src_core']
    target_core = core_data['target_core']

    if src_core in command_commons.skip_cores:
      Logger.info(format("Core '{src_core}' (src) is filtered out."))
      continue
    elif target_core in command_commons.skip_cores:
      Logger.info(format("Core '{target_core}' (target) is filtered out."))
      continue

    core_data = host_cores_map[command_commons.CORE_DATA]
    only_if_cmd = format("test -d {index_location}/snapshot.{src_core}")
    core_root_dir = format("{solr_datadir}/backup_{target_core}")
    core_root_without_backup_dir = format("{solr_datadir}/{target_core}")

    if command_commons.solr_hdfs_path:
      Directory([core_root_dir],
                mode=0755,
                cd_access='a',
                create_parents=True,
                owner=params.infra_solr_user,
                group=params.user_group,
                only_if=only_if_cmd
                )
    else:
      Directory([format("{core_root_dir}/data/index"),
                 format("{core_root_dir}/data/tlog"),
                 format("{core_root_dir}/data/snapshot_metadata")],
                mode=0755,
                cd_access='a',
                create_parents=True,
                owner=params.infra_solr_user,
                group=params.user_group,
                only_if=only_if_cmd
                )

    core_details = core_data[target_core]
    core_properties = {}
    core_properties['numShards'] = command_commons.solr_num_shards
    core_properties['collection.configName'] = "ranger_audits"
    core_properties['name'] = target_core
    core_properties['replicaType'] = core_details['type']
    core_properties['collection'] = command_commons.collection
    core_properties['coreNodeName'] = core_details['node']
    core_properties['shard'] = core_details['shard']
    if command_commons.solr_hdfs_path:
      hdfs_solr_node_folder=command_commons.solr_hdfs_path + format("/backup_{collection}/") + core_details['node']
      source_folder=format("{index_location}/snapshot.{src_core}/")
      if command_commons.check_folder_exists(source_folder):
        hdfs_cores_on_host.append(target_core)
        command_commons.HdfsResource(format("{hdfs_solr_node_folder}/data/index/"),
                                   type="directory",
                                   action="create_on_execute",
                                   source=source_folder,
                                   owner=params.infra_solr_user,
                                   mode=0755,
                                   recursive_chown=True,
                                   recursive_chmod=True
                                   )
        command_commons.HdfsResource(None, action="execute")
        command_commons.HdfsResource(format("{hdfs_solr_node_folder}/data/tlog"),
                                   type="directory",
                                   action="create_on_execute",
                                   owner=params.infra_solr_user,
                                   mode=0755
                                   )
        command_commons.HdfsResource(None, action="execute")
        command_commons.HdfsResource(format("{hdfs_solr_node_folder}/data/snapshot_metadata"),
                                   type="directory",
                                   action="create_on_execute",
                                   owner=params.infra_solr_user,
                                   mode=0755
                                   )
        command_commons.HdfsResource(None, action="execute")
        if command_commons.solr_keep_backup:
          Directory(format("{index_location}/snapshot.{src_core}"),
                  action="delete",
                  only_if=only_if_cmd,
                  owner=params.infra_solr_user)
    else:
      copy_cmd = format(
        "mv {index_location}/snapshot.{src_core}/* {core_root_dir}/data/index/") if command_commons.solr_keep_backup \
        else format("cp -r {index_location}/snapshot.{src_core}/* {core_root_dir}/data/index/")
      Execute(
        copy_cmd, only_if=only_if_cmd,
        user=params.infra_solr_user,
        logoutput=True
      )

    PropertiesFile(
      core_root_dir + '/core.properties',
      properties=core_properties,
      owner=params.infra_solr_user,
      group=params.user_group,
      mode=0644,
      only_if=only_if_cmd
    )

  Execute(format("rm -rf {solr_datadir}/{collection}*"),
          user=params.infra_solr_user,
          logoutput=True)
  for core_data in core_pairs:
    src_core = core_data['src_core']
    target_core = core_data['target_core']

    if src_core in command_commons.skip_cores:
      Logger.info(format("Core '{src_core}' (src) is filtered out."))
      continue
    elif target_core in command_commons.skip_cores:
      Logger.info(format("Core '{target_core}' (target) is filtered out."))
      continue

    core_root_dir = format("{solr_datadir}/backup_{target_core}")
    core_root_without_backup_dir = format("{solr_datadir}/{target_core}")

    if command_commons.solr_hdfs_path:
      if target_core in hdfs_cores_on_host:
        Logger.info(format("Core data '{target_core}' is located on this host, processing..."))
        core_data=host_cores_map[command_commons.CORE_DATA]
        core_details=core_data[target_core]
        core_node=core_details['node']
        collection_core_dir=command_commons.solr_hdfs_path + format("/{collection}/{core_node}")
        backup_collection_core_dir=command_commons.solr_hdfs_path + format("/backup_{collection}/{core_node}")
        command_commons.HdfsResource(collection_core_dir,
                               type="directory",
                               action="delete_on_execute",
                               owner=params.infra_solr_user
                               )
        command_commons.HdfsResource(None, action="execute")
        if command_commons.check_hdfs_folder_exists(backup_collection_core_dir):
          command_commons.move_hdfs_folder(backup_collection_core_dir, collection_core_dir)
      else:
        Logger.info(format("Core data '{target_core}' is not located on this host, skipping..."))

    Execute(
      format("mv {core_root_dir} {core_root_without_backup_dir}"),
      user=params.infra_solr_user,
      logoutput=True,
      only_if=format("test -d {core_root_dir}")
    )

    Directory(
      [format("{core_root_without_backup_dir}")],
      mode=0755,
      cd_access='a',
      create_parents=True,
      owner=params.infra_solr_user,
      group=params.user_group,
      recursive_ownership=True,
      only_if=format("test -d {core_root_without_backup_dir}")
    )
