# Copyright 2014 - Rackspace Hosting
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

"""Solum Deployer Heat handler."""

import logging
import socket
import time

from heatclient import exc
import httplib2
from oslo.config import cfg
from sqlalchemy import exc as sqla_exc
from swiftclient import exceptions as swiftexp
import yaml

from solum.api.handlers import userlog_handler
from solum.common import catalog
from solum.common import clients
from solum.common import exception
from solum.common import heat_utils
from solum.common import repo_utils
from solum.common import solum_swiftclient
from solum import objects
from solum.objects import assembly
from solum.openstack.common import log as openstack_logger
from solum.uploaders import tenant_logger as tlog


LOG = openstack_logger.getLogger(__name__)

STATES = assembly.States


SERVICE_OPTS = [
    cfg.IntOpt('max_attempts',
               default=600,
               help=('Number of attempts to query the Heat stack for '
                     'finding out the status of the created stack and '
                     'getting url of the DU created in the stack')),
    cfg.IntOpt('du_attempts',
               default=500,
               help=('Number of attempts to query the Docker DU for '
                     'finding out the status of the created app and '
                     'getting url of the DU created in the stack')),
    cfg.IntOpt('wait_interval',
               default=1,
               help=('Sleep time interval between two attempts of querying '
                     'the Heat stack. This interval is in seconds.')),
    cfg.FloatOpt('growth_factor',
                 default=1.1,
                 help=('Factor by which sleep time interval increases. '
                       'This value should be >= 1.0')),
    cfg.StrOpt('flavor',
               default="m1.small",
               help='VM Flavor'),
    cfg.StrOpt('image',
               default="coreos",
               help='Image id'),
    cfg.StrOpt('deployer_log_dir',
               default="/var/log/solum/deployer",
               help='Deployer logs location'),
]

cfg.CONF.register_opts(SERVICE_OPTS, group='deployer')
cfg.CONF.import_opt('image_format', 'solum.api.handlers.assembly_handler',
                    group='api')
cfg.CONF.import_group('worker', 'solum.worker.handlers.shell')

deployer_log_dir = cfg.CONF.deployer.deployer_log_dir


def update_assembly(ctxt, assembly_id, data):
    # Here we are updating the assembly synchronously (i.e. without
    # using the conductor). This is because when using the conductor latency
    # is introduced between the update call and when assembly's state is
    # actually updated in the database. This latency leads to concurrency
    # bugs within deployers' actions when multiple deployers are present
    # in the system.
    try:
        objects.registry.Assembly.update_and_save(ctxt, assembly_id, data)
    except sqla_exc.SQLAlchemyError as ex:
        LOG.error("Failed to update assembly status, ID: %s" % assembly_id)
        LOG.exception(ex)


class Handler(object):
    def __init__(self):
        super(Handler, self).__init__()
        objects.load()

    def echo(self, ctxt, message):
        LOG.debug("%s" % message)

    def _get_stack_name(self, assembly, prefix_len=100):
        assem_name = assembly.name
        # heat stack name has a max allowable length of 255
        return ''.join([assem_name[:min(len(assem_name), prefix_len)], '-',
                        assembly.uuid])

    def _delete_app_artifacts_from_swift(self, ctxt, t_logger,
                                         logs_resource_id, assem):
        # Delete image file from swift
        img = objects.registry.Image.get_by_id(ctxt, assem.image_id)
        if img.docker_image_name:
            img_filename = img.docker_image_name.split('-', 1)[1]
            try:
                swift = solum_swiftclient.SwiftClient(ctxt)
                swift.delete_object('solum_du', img_filename)
            except swiftexp.ClientException:
                msg = "Unable to delete DU image from swift."
                t_logger.log(logging.ERROR, msg)
                LOG.debug(msg)
                t_logger.upload()
                return
        img.destroy(ctxt)

        # Delete logs
        try:
            log_handler = userlog_handler.UserlogHandler(ctxt)
            log_handler.delete(logs_resource_id)
        except exception.AuthorizationFailure as authexcp:
            t_logger.log(logging.ERROR, authexcp.message)
            LOG.debug(authexcp.message)
            t_logger.upload()

    def destroy_assembly(self, ctxt, assem_id):
        update_assembly(ctxt, assem_id,
                        {'status': STATES.DELETING})
        assem = objects.registry.Assembly.get_by_id(ctxt, assem_id)
        logs_resource_id = assem.uuid
        stack_id = self._find_id_if_stack_exists(assem)

        # TODO(devkulkarni) Delete t_logger when returning from this call.
        # This needs to be implemented as a decorator since there are
        # multiple return paths from this method.
        t_logger = tlog.TenantLogger(ctxt, assem, deployer_log_dir, 'delete')
        msg = "Deleting Assembly %s" % assem.uuid
        t_logger.log(logging.DEBUG, msg)
        LOG.debug(msg)

        if stack_id is None:
            assem.destroy(ctxt)
            t_logger.upload()
            self._delete_app_artifacts_from_swift(ctxt, t_logger,
                                                  logs_resource_id, assem)
            return
        else:
            osc = clients.OpenStackClients(ctxt)
            try:
                t_logger.log(logging.DEBUG, "Deleting Heat stack.")
                osc.heat().stacks.delete(stack_id)
            except exc.HTTPNotFound:
                # stack already deleted
                assem.destroy(ctxt)
                t_logger.log(logging.ERROR, "Heat stack not found.")
                t_logger.upload()
                self._delete_app_artifacts_from_swift(ctxt, t_logger,
                                                      logs_resource_id, assem)
                return
            except Exception as e:
                LOG.exception(e)
                update_assembly(ctxt, assem_id,
                                {'status': STATES.ERROR_STACK_DELETE_FAILED})
                t_logger.log(logging.ERROR, "Error deleting heat stack.")
                t_logger.upload()
                return

            wait_interval = cfg.CONF.deployer.wait_interval
            growth_factor = cfg.CONF.deployer.growth_factor
            stack_name = self._get_stack_name(assem)
            t_logger.log(logging.DEBUG, "Checking if Heat stack was deleted.")
            for count in range(cfg.CONF.deployer.max_attempts):
                try:
                    # Must use stack_name for expecting a 404
                    osc.heat().stacks.get(stack_name)
                except exc.HTTPNotFound:
                    assem.destroy(ctxt)
                    t_logger.log(logging.DEBUG, "Stack delete successful.")
                    t_logger.upload()
                    self._delete_app_artifacts_from_swift(ctxt, t_logger,
                                                          logs_resource_id,
                                                          assem)
                    return
                time.sleep(wait_interval)
                wait_interval *= growth_factor

            update_assembly(ctxt, assem_id,
                            {'status': STATES.ERROR_STACK_DELETE_FAILED})

            t_logger.log(logging.ERROR, "Error deleting heat stack.")
            t_logger.upload()

    def _destroy_other_assemblies(self, ctxt, assembly_id):
        # Destroy all of an app's READY assemblies except the one named.

        # We query the newly deployed assembly's object here to
        # ensure that we get most up-to-date value for created_at attribute.
        # If we use the already available object then there is a possibility
        # that the attribute does not have the most up-to-date value due to the
        # possibility that SQLAlchemy might not synchronize object's db state
        # with its in-memory representation.
        new_assembly = objects.registry.Assembly.get_by_id(ctxt, assembly_id)

        # Fetch all assemblies by plan id, and self.destroy() them.
        new_assem_id = new_assembly.id
        app_id = new_assembly.plan_id
        created_at = new_assembly.created_at
        assemblies = objects.registry.AssemblyList.get_earlier(new_assem_id,
                                                               app_id,
                                                               STATES.READY,
                                                               created_at)
        for assem in assemblies:
            if assem.id != new_assembly.id:
                self.destroy_assembly(ctxt, assem.id)

    def destroy_app(self, ctxt, app_id):
        # Destroy a plan's assemblies, and then the plan.
        plan = objects.registry.Plan.get_by_id(ctxt, app_id)

        # Fetch all assemblies by plan id, and self.destroy() them.
        assemblies = objects.registry.AssemblyList.get_all(ctxt)
        for assem in assemblies:
            if app_id == assem.plan_id:
                self.destroy_assembly(ctxt, assem.id)

        plan.destroy(ctxt)

    def deploy(self, ctxt, assembly_id, image_loc, image_name, ports):
        osc = clients.OpenStackClients(ctxt)

        assem = objects.registry.Assembly.get_by_id(ctxt,
                                                    assembly_id)

        # TODO(devkulkarni) Delete t_logger when returning from this call.
        # This needs to be implemented as a decorator since there are
        # multiple return paths from this method.
        t_logger = tlog.TenantLogger(ctxt, assem, deployer_log_dir, 'deploy')
        msg = "Deploying Assembly %s" % assem.uuid
        t_logger.log(logging.DEBUG, msg)

        LOG.debug("Image loc:%s, image_name:%s" % (image_loc, image_name))

        parameters = self._get_parameters(ctxt, cfg.CONF.api.image_format,
                                          image_loc, image_name, assem,
                                          ports, osc, t_logger)
        LOG.debug(parameters)

        if parameters is None:
            return

        template = self._get_template(ctxt, cfg.CONF.api.image_format,
                                      cfg.CONF.worker.image_storage, image_loc,
                                      image_name, assem, ports, t_logger)
        LOG.debug(template)

        if template is None:
            return

        stack_name = self._get_stack_name(assem)
        stack_id = self._find_id_if_stack_exists(assem)

        if assem.status == STATES.DELETING:
            t_logger.log(logging.DEBUG, "Assembly being deleted..returning")
            t_logger.upload()
            return

        if stack_id is not None:
            try:
                osc.heat().stacks.update(stack_id,
                                         stack_name=stack_name,
                                         template=template,
                                         parameters=parameters)

            except Exception as e:
                LOG.error("Error updating Heat Stack for,"
                          " assembly %s" % assembly_id)
                LOG.exception(e)
                update_assembly(ctxt, assembly_id, {'status': STATES.ERROR})
                t_logger.log(logging.ERROR, "Error updating heat stack.")
                t_logger.upload()
                return
        else:
            try:
                getfile_key = "robust-du-handling.sh"
                file_cnt = None

                try:
                    file_cnt = catalog.get_from_contrib(getfile_key)
                except exception.ObjectNotFound as onf_ex:
                    LOG.exception(onf_ex)
                    update_assembly(ctxt, assem.id, {'status': STATES.ERROR})
                    t_logger.log(logging.ERROR, "Error reading %s"
                                 % getfile_key)
                    t_logger.upload()
                    return

                get_file_dict = {}
                get_file_dict[getfile_key] = file_cnt

                created_stack = osc.heat().stacks.create(stack_name=stack_name,
                                                         template=template,
                                                         parameters=parameters,
                                                         files=get_file_dict)
            except Exception as exp:
                LOG.error("Error creating Heat Stack for,"
                          " assembly %s" % assembly_id)
                LOG.exception(exp)
                update_assembly(ctxt, assembly_id,
                                {'status': STATES.ERROR_STACK_CREATE_FAILED})
                t_logger.log(logging.ERROR, "Error creating heat stack.")
                t_logger.upload()
                return
            stack_id = created_stack['stack']['id']

            LOG.debug("Stack id: %s" % stack_id)

            comp_name = 'Heat_Stack_for_%s' % assem.name
            comp_description = 'Heat Stack %s' % (
                yaml.load(template).get('description'))
            try:
                objects.registry.Component.assign_and_create(
                    ctxt, assem, comp_name, 'heat_stack', comp_description,
                    created_stack['stack']['links'][0]['href'], stack_id)
            except sqla_exc.IntegrityError:
                LOG.error("IntegrityError in creating Heat Stack component,"
                          " assembly %s may be deleted" % assembly_id)
                update_assembly(ctxt, assembly_id, {'status': STATES.ERROR})
                t_logger.log(logging.ERROR, "Error creating heat stack.")
                t_logger.upload()
                return
        update_assembly(ctxt, assembly_id, {'status': STATES.DEPLOYING})

        result = self._check_stack_status(ctxt, assembly_id, osc, stack_id,
                                          ports, t_logger)
        assem.status = result
        t_logger.upload()
        if result == STATES.READY:
            self._destroy_other_assemblies(ctxt, assembly_id)

    def _get_template(self, ctxt, image_format, image_storage,
                      image_loc, image_name, assem, ports, t_logger):
        template = None

        if image_format == 'docker':
            try:
                template = catalog.get('templates', 'basic')
            except exception.ObjectNotFound as onf_ex:
                LOG.exception(onf_ex)
                update_assembly(ctxt, assem.id, {'status': STATES.ERROR})
                t_logger.log(logging.ERROR, "Error reading heat template.")
                t_logger.upload()
                return template

        elif image_format == 'vm':
            if image_storage == 'glance':
                msg = ("image_storage %s not supported with image_format %s" %
                       (image_storage, image_format))
                LOG.debug(msg)
                update_assembly(ctxt, assem.id, {'status': STATES.ERROR})
                t_logger.log(logging.DEBUG, "Solum config error: %s " % msg)
                t_logger.upload()
            else:
                try:
                    template = catalog.get('templates', 'coreos')
                except exception.ObjectNotFound as onf_ex:
                    LOG.exception(onf_ex)
                    update_assembly(ctxt, assem.id, {'status': STATES.ERROR})
                    t_logger.log(logging.ERROR, "Error reading heat template.")
                    t_logger.upload()
                    return template

                if image_storage == 'docker_registry':
                    template = self._get_template_for_docker_reg(
                        assem, template, image_loc, image_name, ports)
        else:
            LOG.debug("Image format %s is not supported." % image_format)
            update_assembly(ctxt, assem.id, {'status': STATES.ERROR})
            t_logger.log(logging.DEBUG, "Solum config error: Image format.")
            t_logger.upload()

        return template

    def _get_parameters(self, ctxt, image_format, image_loc, image_name,
                        assem, ports, osc, t_logger):
        parameters = None
        if image_format == 'docker':
            glance_img_uuid = image_loc
            LOG.debug("Image id:%s" % glance_img_uuid)
            LOG.debug("Specified ports:%s" % ports)
            LOG.debug("Picking first port..")
            port_to_use = ports[0]
            LOG.debug("Application port:%s" % port_to_use)

            parameters = {'app_name': assem.name,
                          'image': glance_img_uuid,
                          'port': port_to_use}
            parameters.update(heat_utils.get_network_parameters(osc))

        elif image_format == 'vm':
            parameters = {'name': str(assem.uuid),
                          'flavor': cfg.CONF.deployer.flavor,
                          'image': cfg.CONF.deployer.image}
            ports_str = ''
            for port in ports:
                ports_str += ' -p {pt}:{pt}'.format(pt=port)

            parameters['location'] = image_loc
            parameters['du'] = image_name
            parameters['publish_ports'] = ports_str.strip()
        else:
            LOG.debug("Image format %s is not supported." % image_format)
            update_assembly(ctxt, assem.id, {'status': STATES.ERROR})
            t_logger.log(logging.DEBUG, "Solum config error: Image format.")
            t_logger.upload()
        return parameters

    def _check_stack_status(self, ctxt, assembly_id, osc, stack_id, ports,
                            t_logger):

        wait_interval = cfg.CONF.deployer.wait_interval
        growth_factor = cfg.CONF.deployer.growth_factor

        stack = None

        for count in range(cfg.CONF.deployer.max_attempts):
            time.sleep(wait_interval)
            wait_interval *= growth_factor
            try:
                stack = osc.heat().stacks.get(stack_id)
            except Exception as e:
                LOG.exception(e)
                continue

            if stack.status == 'COMPLETE':
                break
            elif stack.status == 'FAILED':
                update_assembly(ctxt, assembly_id,
                                {'status': STATES.ERROR_STACK_CREATE_FAILED})
                lg_msg = "App deployment failed: Heat stack creation failure"
                t_logger.log(logging.ERROR, lg_msg)
                return STATES.ERROR_STACK_CREATE_FAILED

        if stack is None or (stack and stack.status == ""):
            update_assembly(ctxt, assembly_id,
                            {'status': STATES.ERROR_STACK_CREATE_FAILED})
            lg_msg = "App deployment failed: Heat stack is in unexpected state"
            t_logger.log(logging.ERROR, lg_msg)
            return STATES.ERROR_STACK_CREATE_FAILED

        host_ip = self._parse_server_url(stack)
        if host_ip is None:
            LOG.exception("Could not parse url from heat stack.")
            update_assembly(ctxt, assembly_id,
                            {'status': STATES.ERROR})
            lg_msg = ("App deployment failed: "
                      "container IP address not available")
            t_logger.log(logging.ERROR, lg_msg)
            return STATES.ERROR

        app_uri = host_ip

        if len(ports) == 1:
            app_uri += ":" + str(ports[0])
        if len(ports) > 1:
            port_list = ','.join(str(p) for p in ports)
            app_uri += ":[" + port_list + "]"

        to_upd = {'status': STATES.STARTING_APP,
                  'application_uri': app_uri}
        update_assembly(ctxt, assembly_id, to_upd)
        LOG.debug("Application URI: %s" % app_uri)

        successful_ports = set()
        du_is_up = False
        for count in range(cfg.CONF.deployer.du_attempts):
            for prt in ports:
                if prt not in successful_ports:
                    du_url = 'http://{host}:{port}'.format(host=host_ip,
                                                           port=prt)
                    try:
                        if repo_utils.is_reachable(du_url):
                            successful_ports.add(prt)
                            if len(successful_ports) == len(ports):
                                du_is_up = True
                                break
                    except socket.timeout:
                        LOG.debug("Connection to %s timed out"
                                  "assembly ID: %s" % (du_url, assembly_id))
                    except (httplib2.HttpLib2Error, socket.error) as serr:
                        if count % 5 == 0:
                            LOG.exception(serr)
                        else:
                            LOG.debug(".")
                    except Exception as exp:
                        LOG.exception(exp)
                        update_assembly(ctxt, assembly_id,
                                        {'status': STATES.ERROR})
                        lg_msg = ("App deployment error: unexpected error "
                                  " when trying to reach app endpoint")
                        t_logger.log(logging.ERROR, lg_msg)
                        return STATES.ERROR
            if du_is_up:
                break
            time.sleep(1)

        if du_is_up:
            to_update = {'status': STATES.READY}
        else:
            to_update = {'status': STATES.ERROR_CODE_DEPLOYMENT}
            lg_msg = ("App deployment error: unreachable server or port, "
                      " please check your port config.")
            t_logger.log(logging.ERROR, lg_msg)
        update_assembly(ctxt, assembly_id, to_update)
        return to_update['status']

    def _parse_server_url(self, heat_output):
        """Parse server url from heat-stack-show output."""
        if 'outputs' in heat_output._info:
            return heat_output._info['outputs'][0]['output_value']
        return None

    def _find_id_if_stack_exists(self, assem):
        if assem.heat_stack_component is not None:
            return assem.heat_stack_component.heat_stack_id
        return None

    def _get_template_for_docker_reg(self, assem, template,
                                     image_loc, image_name, ports):
        du_name = image_loc
        ports_str = ''
        for port in ports:
            ports_str += ' -p {pt}:{pt}'.format(pt=port)
        run_docker_str = ('#!/bin/bash -x\n'
                          '# Invoke the container\n'
                          'docker run {publish_ports} -d {du}\n'
                          'wc_notify --data-binary {stat}')
        run_docker = run_docker_str.format(publish_ports=ports_str.strip(),
                                           du=du_name,
                                           stat='\'{"status": "SUCCESS"}\'')

        LOG.debug("run_docker:%s" % run_docker)

        template_bdy = yaml.safe_load(template)
        comp_instance = template_bdy['resources']['compute_instance']
        user_data = comp_instance['properties']['user_data']
        user_data['str_replace']['template'] = run_docker
        comp_instance['properties']['user_data'] = user_data
        template_bdy['resources']['compute_instance'] = comp_instance
        template = yaml.dump(template_bdy)
        return template
