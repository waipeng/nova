# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Cloud Controller: Implementation of EC2 REST API calls, which are
dispatched to other nodes via AMQP RPC. State is via distributed
datastore.
"""

import base64
import logging
import os
import time

from nova import datastore
from nova import exception
from nova import flags
from nova import rpc
from nova import utils
from nova.auth import manager
from nova.compute import model
from nova.compute.instance_types import INSTANCE_TYPES
from nova.api.ec2 import images
from nova.network import service as network_service
from nova.network import model as network_model
from nova.volume import service


FLAGS = flags.FLAGS


def _gen_key(user_id, key_name):
    """ Tuck this into AuthManager """
    mgr = manager.AuthManager()
    private_key, fingerprint = mgr.generate_key_pair(user_id, key_name)
    return {'private_key': private_key, 'fingerprint': fingerprint}


class CloudController(object):
    """ CloudController provides the critical dispatch between
 inbound API calls through the endpoint and messages
 sent to the other nodes.
"""
    def __init__(self):
        self.instdir = model.InstanceDirectory()
        self.setup()

    @property
    def instances(self):
        """ All instances in the system, as dicts """
        return self.instdir.all

    @property
    def volumes(self):
        """ returns a list of all volumes """
        for volume_id in datastore.Redis.instance().smembers("volumes"):
            volume = service.get_volume(volume_id)
            yield volume

    def __str__(self):
        return 'CloudController'

    def setup(self):
        """ Ensure the keychains and folders exist. """
        # Create keys folder, if it doesn't exist
        if not os.path.exists(FLAGS.keys_path):
            os.makedirs(FLAGS.keys_path)
        # Gen root CA, if we don't have one
        root_ca_path = os.path.join(FLAGS.ca_path, FLAGS.ca_file)
        if not os.path.exists(root_ca_path):
            start = os.getcwd()
            os.chdir(FLAGS.ca_path)
            utils.runthis("Generating root CA: %s", "sh genrootca.sh")
            os.chdir(start)
            # TODO: Do this with M2Crypto instead

    def get_instance_by_ip(self, ip):
        return self.instdir.by_ip(ip)

    def _get_mpi_data(self, project_id):
        result = {}
        for instance in self.instdir.all:
            if instance['project_id'] == project_id:
                line = '%s slots=%d' % (instance['private_dns_name'],
                    INSTANCE_TYPES[instance['instance_type']]['vcpus'])
                if instance['key_name'] in result:
                    result[instance['key_name']].append(line)
                else:
                    result[instance['key_name']] = [line]
        return result

    def get_metadata(self, ipaddress):
        i = self.get_instance_by_ip(ipaddress)
        if i is None:
            return None
        mpi = self._get_mpi_data(i['project_id'])
        if i['key_name']:
            keys = {
                '0': {
                    '_name': i['key_name'],
                    'openssh-key': i['key_data']
                }
            }
        else:
            keys = ''

        address_record = network_model.FixedIp(i['private_dns_name'])
        if address_record:
            hostname = address_record['hostname']
        else:
            hostname = 'ip-%s' % i['private_dns_name'].replace('.', '-')
        data = {
            'user-data': base64.b64decode(i['user_data']),
            'meta-data': {
                'ami-id': i['image_id'],
                'ami-launch-index': i['ami_launch_index'],
                'ami-manifest-path': 'FIXME', # image property
                'block-device-mapping': { # TODO: replace with real data
                    'ami': 'sda1',
                    'ephemeral0': 'sda2',
                    'root': '/dev/sda1',
                    'swap': 'sda3'
                },
                'hostname': hostname,
                'instance-action': 'none',
                'instance-id': i['instance_id'],
                'instance-type': i.get('instance_type', ''),
                'local-hostname': hostname,
                'local-ipv4': i['private_dns_name'], # TODO: switch to IP
                'kernel-id': i.get('kernel_id', ''),
                'placement': {
                    'availaibility-zone': i.get('availability_zone', 'nova'),
                },
                'public-hostname': hostname,
                'public-ipv4': i.get('dns_name', ''), # TODO: switch to IP
                'public-keys': keys,
                'ramdisk-id': i.get('ramdisk_id', ''),
                'reservation-id': i['reservation_id'],
                'security-groups': i.get('groups', ''),
                'mpi': mpi
            }
        }
        if False: # TODO: store ancestor ids
            data['ancestor-ami-ids'] = []
        if i.get('product_codes', None):
            data['product-codes'] = i['product_codes']
        return data

    def describe_availability_zones(self, context, **kwargs):
        return {'availabilityZoneInfo': [{'zoneName': 'nova',
                                          'zoneState': 'available'}]}

    def describe_regions(self, context, region_name=None, **kwargs):
        # TODO(vish): region_name is an array.  Support filtering
        return {'regionInfo': [{'regionName': 'nova',
                                'regionUrl': FLAGS.ec2_url}]}

    def describe_snapshots(self,
                           context,
                           snapshot_id=None,
                           owner=None,
                           restorable_by=None,
                           **kwargs):
        return {'snapshotSet': [{'snapshotId': 'fixme',
                                 'volumeId': 'fixme',
                                 'status': 'fixme',
                                 'startTime': 'fixme',
                                 'progress': 'fixme',
                                 'ownerId': 'fixme',
                                 'volumeSize': 0,
                                 'description': 'fixme'}]}

    def describe_key_pairs(self, context, key_name=None, **kwargs):
        key_pairs = context.user.get_key_pairs()
        if not key_name is None:
            key_pairs = [x for x in key_pairs if x.name in key_name]

        result = []
        for key_pair in key_pairs:
            # filter out the vpn keys
            suffix = FLAGS.vpn_key_suffix
            if context.user.is_admin() or not key_pair.name.endswith(suffix):
                result.append({
                    'keyName': key_pair.name,
                    'keyFingerprint': key_pair.fingerprint,
                })

        return {'keypairsSet': result}

    def create_key_pair(self, context, key_name, **kwargs):
        data = _gen_key(context.user.id, key_name)
        return {'keyName': key_name,
                'keyFingerprint': data['fingerprint'],
                'keyMaterial': data['private_key']}

    def delete_key_pair(self, context, key_name, **kwargs):
        context.user.delete_key_pair(key_name)
        # aws returns true even if the key doens't exist
        return True

    def describe_security_groups(self, context, group_names, **kwargs):
        groups = {'securityGroupSet': []}

        # Stubbed for now to unblock other things.
        return groups

    def create_security_group(self, context, group_name, **kwargs):
        return True

    def delete_security_group(self, context, group_name, **kwargs):
        return True

    def get_console_output(self, context, instance_id, **kwargs):
        # instance_id is passed in as a list of instances
        instance = self._get_instance(context, instance_id[0])
        return rpc.call('%s.%s' % (FLAGS.compute_topic, instance['node_name']),
            {"method": "get_console_output",
             "args": {"instance_id": instance_id[0]}})

    def _get_user_id(self, context):
        if context and context.user:
            return context.user.id
        else:
            return None

    def describe_volumes(self, context, **kwargs):
        volumes = []
        for volume in self.volumes:
            if context.user.is_admin() or volume['project_id'] == context.project.id:
                v = self.format_volume(context, volume)
                volumes.append(v)
        return {'volumeSet': volumes}

    def format_volume(self, context, volume):
        v = {}
        v['volumeId'] = volume['volume_id']
        v['status'] = volume['status']
        v['size'] = volume['size']
        v['availabilityZone'] = volume['availability_zone']
        v['createTime'] = volume['create_time']
        if context.user.is_admin():
            v['status'] = '%s (%s, %s, %s, %s)' % (
                volume.get('status', None),
                volume.get('user_id', None),
                volume.get('node_name', None),
                volume.get('instance_id', ''),
                volume.get('mountpoint', ''))
        if volume['attach_status'] == 'attached':
            v['attachmentSet'] = [{'attachTime': volume['attach_time'],
                                   'deleteOnTermination': volume['delete_on_termination'],
                                   'device': volume['mountpoint'],
                                   'instanceId': volume['instance_id'],
                                   'status': 'attached',
                                   'volume_id': volume['volume_id']}]
        else:
            v['attachmentSet'] = [{}]
        return v

    def create_volume(self, context, size, **kwargs):
        # TODO(vish): refactor this to create the volume object here and tell service to create it
        result = rpc.call(FLAGS.volume_topic, 
                {"method": "create_volume",
                 "args": {"size": size,
                          "user_id": context.user.id,
                          "project_id": context.project.id}})
        # NOTE(vish): rpc returned value is in the result key in the dictionary
        volume = self._get_volume(context, result)
        return {'volumeSet': [self.format_volume(context, volume)]}

    def _get_address(self, context, public_ip):
        # FIXME(vish) this should move into network.py
        address = network_model.ElasticIp.lookup(public_ip)
        if address and (context.user.is_admin() or address['project_id'] == context.project.id):
            return address
        raise exception.NotFound("Address at ip %s not found" % public_ip)

    def _get_image(self, context, image_id):
        """passes in context because
        objectstore does its own authorization"""
        result = images.list(context, [image_id])
        if not result:
            raise exception.NotFound('Image %s could not be found' % image_id)
        image = result[0]
        return image

    def _get_instance(self, context, instance_id):
        for instance in self.instdir.all:
            if instance['instance_id'] == instance_id:
                if context.user.is_admin() or instance['project_id'] == context.project.id:
                    return instance
        raise exception.NotFound('Instance %s could not be found' % instance_id)

    def _get_volume(self, context, volume_id):
        volume = service.get_volume(volume_id)
        if context.user.is_admin() or volume['project_id'] == context.project.id:
            return volume
        raise exception.NotFound('Volume %s could not be found' % volume_id)

    def attach_volume(self, context, volume_id, instance_id, device, **kwargs):
        volume = self._get_volume(context, volume_id)
        if volume['status'] == "attached":
            raise exception.ApiError("Volume is already attached")
        # TODO(vish): looping through all volumes is slow. We should probably maintain an index
        for vol in self.volumes:
            if vol['instance_id'] == instance_id and vol['mountpoint'] == device:
                raise exception.ApiError("Volume %s is already attached to %s" % (vol['volume_id'], vol['mountpoint']))
        volume.start_attach(instance_id, device)
        instance = self._get_instance(context, instance_id)
        compute_node = instance['node_name']
        rpc.cast('%s.%s' % (FLAGS.compute_topic, compute_node),
                                {"method": "attach_volume",
                                 "args": {"volume_id": volume_id,
                                           "instance_id": instance_id,
                                           "mountpoint": device}})
        return {'attachTime': volume['attach_time'],
                'device': volume['mountpoint'],
                'instanceId': instance_id,
                'requestId': context.request_id,
                'status': volume['attach_status'],
                'volumeId': volume_id}

    def detach_volume(self, context, volume_id, **kwargs):
        volume = self._get_volume(context, volume_id)
        instance_id = volume.get('instance_id', None)
        if not instance_id:
            raise exception.Error("Volume isn't attached to anything!")
        if volume['status'] == "available":
            raise exception.Error("Volume is already detached")
        try:
            volume.start_detach()
            instance = self._get_instance(context, instance_id)
            rpc.cast('%s.%s' % (FLAGS.compute_topic, instance['node_name']),
                                {"method": "detach_volume",
                                 "args": {"instance_id": instance_id,
                                           "volume_id": volume_id}})
        except exception.NotFound:
            # If the instance doesn't exist anymore,
            # then we need to call detach blind
            volume.finish_detach()
        return {'attachTime': volume['attach_time'],
                'device': volume['mountpoint'],
                'instanceId': instance_id,
                'requestId': context.request_id,
                'status': volume['attach_status'],
                'volumeId': volume_id}

    def _convert_to_set(self, lst, label):
        if lst == None or lst == []:
            return None
        if not isinstance(lst, list):
            lst = [lst]
        return [{label: x} for x in lst]

    def describe_instances(self, context, **kwargs):
        return self._format_describe_instances(context)

    def _format_describe_instances(self, context):
        return { 'reservationSet': self._format_instances(context) }

    def _format_run_instances(self, context, reservation_id):
        i = self._format_instances(context, reservation_id)
        assert len(i) == 1
        return i[0]

    def _format_instances(self, context, reservation_id = None):
        reservations = {}
        if context.user.is_admin():
            instgenerator = self.instdir.all
        else:
            instgenerator = self.instdir.by_project(context.project.id)
        for instance in instgenerator:
            res_id = instance.get('reservation_id', 'Unknown')
            if reservation_id != None and reservation_id != res_id:
                continue
            if not context.user.is_admin():
                if instance['image_id'] == FLAGS.vpn_image_id:
                    continue
            i = {}
            i['instance_id'] = instance.get('instance_id', None)
            i['image_id'] = instance.get('image_id', None)
            i['instance_state'] = {
                'code': instance.get('state', 0),
                'name': instance.get('state_description', 'pending')
            }
            i['public_dns_name'] = network_model.get_public_ip_for_instance(
                                                        i['instance_id'])
            i['private_dns_name'] = instance.get('private_dns_name', None)
            if not i['public_dns_name']:
                i['public_dns_name'] = i['private_dns_name']
            i['dns_name'] = instance.get('dns_name', None)
            i['key_name'] = instance.get('key_name', None)
            if context.user.is_admin():
                i['key_name'] = '%s (%s, %s)' % (i['key_name'],
                    instance.get('project_id', None),
                    instance.get('node_name', ''))
            i['product_codes_set'] = self._convert_to_set(
                instance.get('product_codes', None), 'product_code')
            i['instance_type'] = instance.get('instance_type', None)
            i['launch_time'] = instance.get('launch_time', None)
            i['ami_launch_index'] = instance.get('ami_launch_index',
                                                 None)
            if not reservations.has_key(res_id):
                r = {}
                r['reservation_id'] = res_id
                r['owner_id'] = instance.get('project_id', None)
                r['group_set'] = self._convert_to_set(
                    instance.get('groups', None), 'group_id')
                r['instances_set'] = []
                reservations[res_id] = r
            reservations[res_id]['instances_set'].append(i)

        return list(reservations.values())

    def describe_addresses(self, context, **kwargs):
        return self.format_addresses(context)

    def format_addresses(self, context):
        addresses = []
        for address in network_model.ElasticIp.all():
            # TODO(vish): implement a by_project iterator for addresses
            if (context.user.is_admin() or
                address['project_id'] == context.project.id):
                address_rv = {
                    'public_ip': address['address'],
                    'instance_id': address.get('instance_id', 'free')
                }
                if context.user.is_admin():
                    address_rv['instance_id'] = "%s (%s, %s)" % (
                        address['instance_id'],
                        address['user_id'],
                        address['project_id'],
                    )
            addresses.append(address_rv)
        return {'addressesSet': addresses}

    def allocate_address(self, context, **kwargs):
        network_topic = self._get_network_topic(context)
        public_ip = rpc.call(network_topic,
                         {"method": "allocate_elastic_ip",
                          "args": {"user_id": context.user.id,
                                   "project_id": context.project.id}})
        return {'addressSet': [{'publicIp': public_ip}]}

    def release_address(self, context, public_ip, **kwargs):
        # NOTE(vish): Should we make sure this works?
        network_topic = self._get_network_topic(context)
        rpc.cast(network_topic,
                         {"method": "deallocate_elastic_ip",
                          "args": {"elastic_ip": public_ip}})
        return {'releaseResponse': ["Address released."]}

    def associate_address(self, context, instance_id, public_ip, **kwargs):
        instance = self._get_instance(context, instance_id)
        address = self._get_address(context, public_ip)
        network_topic = self._get_network_topic(context)
        rpc.cast(network_topic,
                         {"method": "associate_elastic_ip",
                          "args": {"elastic_ip": address['address'],
                                   "fixed_ip": instance['private_dns_name'],
                                   "instance_id": instance['instance_id']}})
        return {'associateResponse': ["Address associated."]}

    def disassociate_address(self, context, public_ip, **kwargs):
        address = self._get_address(context, public_ip)
        network_topic = self._get_network_topic(context)
        rpc.cast(network_topic,
                         {"method": "disassociate_elastic_ip",
                          "args": {"elastic_ip": address['address']}})
        return {'disassociateResponse': ["Address disassociated."]}

    def _get_network_topic(self, context):
        """Retrieves the network host for a project"""
        host = network_service.get_host_for_project(context.project.id)
        if not host:
            host = rpc.call(FLAGS.network_topic,
                                    {"method": "set_network_host",
                                     "args": {"user_id": context.user.id,
                                              "project_id": context.project.id}})
        return '%s.%s' %(FLAGS.network_topic, host)

    def run_instances(self, context, **kwargs):
        # make sure user can access the image
        # vpn image is private so it doesn't show up on lists
        if kwargs['image_id'] != FLAGS.vpn_image_id:
            image = self._get_image(context, kwargs['image_id'])

        # FIXME(ja): if image is cloudpipe, this breaks

        # get defaults from imagestore
        image_id = image['imageId']
        kernel_id = image.get('kernelId', FLAGS.default_kernel)
        ramdisk_id = image.get('ramdiskId', FLAGS.default_ramdisk)

        # API parameters overrides of defaults
        kernel_id = kwargs.get('kernel_id', kernel_id)
        ramdisk_id = kwargs.get('ramdisk_id', ramdisk_id)

        # make sure we have access to kernel and ramdisk
        self._get_image(context, kernel_id)
        self._get_image(context, ramdisk_id)

        logging.debug("Going to run instances...")
        reservation_id = utils.generate_uid('r')
        launch_time = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        key_data = None
        if kwargs.has_key('key_name'):
            key_pair = context.user.get_key_pair(kwargs['key_name'])
            if not key_pair:
                raise exception.ApiError('Key Pair %s not found' %
                                         kwargs['key_name'])
            key_data = key_pair.public_key
        network_topic = self._get_network_topic(context)
        # TODO: Get the real security group of launch in here
        security_group = "default"
        for num in range(int(kwargs['max_count'])):
            is_vpn = False
            if image_id  == FLAGS.vpn_image_id:
                is_vpn = True
            inst = self.instdir.new()
            allocate_data = rpc.call(network_topic,
                     {"method": "allocate_fixed_ip",
                      "args": {"user_id": context.user.id,
                               "project_id": context.project.id,
                               "security_group": security_group,
                               "is_vpn": is_vpn,
                               "hostname": inst.instance_id}})
            inst['image_id'] = image_id
            inst['kernel_id'] = kernel_id
            inst['ramdisk_id'] = ramdisk_id
            inst['user_data'] = kwargs.get('user_data', '')
            inst['instance_type'] = kwargs.get('instance_type', 'm1.small')
            inst['reservation_id'] = reservation_id
            inst['launch_time'] = launch_time
            inst['key_data'] = key_data or ''
            inst['key_name'] = kwargs.get('key_name', '')
            inst['user_id'] = context.user.id
            inst['project_id'] = context.project.id
            inst['ami_launch_index'] = num
            inst['security_group'] = security_group
            inst['hostname'] = inst.instance_id
            for (key, value) in allocate_data.iteritems():
                inst[key] = value

            inst.save()
            rpc.cast(FLAGS.compute_topic,
                 {"method": "run_instance",
                  "args": {"instance_id": inst.instance_id}})
            logging.debug("Casting to node for %s's instance with IP of %s" %
                      (context.user.name, inst['private_dns_name']))
        # TODO: Make Network figure out the network name from ip.
        return self._format_run_instances(context, reservation_id)

    def terminate_instances(self, context, instance_id, **kwargs):
        logging.debug("Going to start terminating instances")
        network_topic = self._get_network_topic(context)
        for i in instance_id:
            logging.debug("Going to try and terminate %s" % i)
            try:
                instance = self._get_instance(context, i)
            except exception.NotFound:
                logging.warning("Instance %s was not found during terminate"
                                % i)
                continue
            elastic_ip = network_model.get_public_ip_for_instance(i)
            if elastic_ip:
                logging.debug("Disassociating address %s" % elastic_ip)
                # NOTE(vish): Right now we don't really care if the ip is
                #             disassociated.  We may need to worry about
                #             checking this later.  Perhaps in the scheduler?
                rpc.cast(network_topic,
                         {"method": "disassociate_elastic_ip",
                          "args": {"elastic_ip": elastic_ip}})

            fixed_ip = instance.get('private_dns_name', None)
            if fixed_ip:
                logging.debug("Deallocating address %s" % fixed_ip)
                # NOTE(vish): Right now we don't really care if the ip is
                #             actually removed.  We may need to worry about
                #             checking this later.  Perhaps in the scheduler?
                rpc.cast(network_topic,
                         {"method": "deallocate_fixed_ip",
                          "args": {"fixed_ip": fixed_ip}})

            if instance.get('node_name', 'unassigned') != 'unassigned':
                # NOTE(joshua?): It's also internal default
                rpc.cast('%s.%s' % (FLAGS.compute_topic, instance['node_name']),
                         {"method": "terminate_instance",
                          "args": {"instance_id": i}})
            else:
                instance.destroy()
        return True

    def reboot_instances(self, context, instance_id, **kwargs):
        """instance_id is a list of instance ids"""
        for i in instance_id:
            instance = self._get_instance(context, i)
            rpc.cast('%s.%s' % (FLAGS.compute_topic, instance['node_name']),
                             {"method": "reboot_instance",
                              "args": {"instance_id": i}})
        return True

    def delete_volume(self, context, volume_id, **kwargs):
        # TODO: return error if not authorized
        volume = self._get_volume(context, volume_id)
        volume_node = volume['node_name']
        rpc.cast('%s.%s' % (FLAGS.volume_topic, volume_node),
                            {"method": "delete_volume",
                             "args": {"volume_id": volume_id}})
        return True

    def describe_images(self, context, image_id=None, **kwargs):
        # The objectstore does its own authorization for describe
        imageSet = images.list(context, image_id)
        return {'imagesSet': imageSet}

    def deregister_image(self, context, image_id, **kwargs):
        # FIXME: should the objectstore be doing these authorization checks?
        images.deregister(context, image_id)
        return {'imageId': image_id}

    def register_image(self, context, image_location=None, **kwargs):
        # FIXME: should the objectstore be doing these authorization checks?
        if image_location is None and kwargs.has_key('name'):
            image_location = kwargs['name']
        image_id = images.register(context, image_location)
        logging.debug("Registered %s as %s" % (image_location, image_id))
        return {'imageId': image_id}

    def describe_image_attribute(self, context, image_id, attribute, **kwargs):
        if attribute != 'launchPermission':
            raise exception.ApiError('attribute not supported: %s' % attribute)
        try:
            image = images.list(context, image_id)[0]
        except IndexError:
            raise exception.ApiError('invalid id: %s' % image_id)
        result = {'image_id': image_id, 'launchPermission': []}
        if image['isPublic']:
            result['launchPermission'].append({'group': 'all'})
        return result

    def modify_image_attribute(self, context, image_id, attribute, operation_type, **kwargs):
        # TODO(devcamcar): Support users and groups other than 'all'.
        if attribute != 'launchPermission':
            raise exception.ApiError('attribute not supported: %s' % attribute)
        if not 'user_group' in kwargs:
            raise exception.ApiError('user or group not specified')
        if len(kwargs['user_group']) != 1 and kwargs['user_group'][0] != 'all':
            raise exception.ApiError('only group "all" is supported')
        if not operation_type in ['add', 'remove']:
            raise exception.ApiError('operation_type must be add or remove')
        return images.modify(context, image_id, operation_type)

    def update_state(self, topic, value):
        """ accepts status reports from the queue and consolidates them """
        # TODO(jmc): if an instance has disappeared from
        # the node, call instance_death
        if topic == "instances":
            return True
        aggregate_state = getattr(self, topic)
        node_name = value.keys()[0]
        items = value[node_name]

        logging.debug("Updating %s state for %s" % (topic, node_name))

        for item_id in items.keys():
            if (aggregate_state.has_key('pending') and
                aggregate_state['pending'].has_key(item_id)):
                del aggregate_state['pending'][item_id]
        aggregate_state[node_name] = items
        return True
