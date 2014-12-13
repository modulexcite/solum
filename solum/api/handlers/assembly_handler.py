# Copyright 2013 - Rackspace
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import uuid

import httplib2
from oslo.config import cfg

from solum.api.handlers import handler
from solum.common import context
from solum.common import exception
from solum.common import solum_keystoneclient
from solum.deployer import api as deploy_api
from solum import objects
from solum.objects import assembly
from solum.objects import image
from solum.openstack.common import log as logging
from solum.worker import api

# Register options for the service
API_SERVICE_OPTS = [
    cfg.StrOpt('image_format',
               default='qcow2',
               help='The format of the image to output'),
    cfg.StrOpt('source_format',
               default='heroku',
               help='The format of source repository'),
    cfg.StrOpt('rebuild_phrase',
               default='solum retry tests',
               help='Comment phrase to trigger rebuilding'),
]

LOG = logging.getLogger(__name__)
CONF = cfg.CONF
opt_group = cfg.OptGroup(name='api',
                         title='Options for the solum-api service')
CONF.register_group(opt_group)
CONF.register_opts(API_SERVICE_OPTS, opt_group)

ASSEMBLY_STATES = assembly.States
IMAGE_STATES = image.States


class AssemblyHandler(handler.Handler):
    """Fulfills a request on the assembly resource."""

    def get(self, id):
        """Return an assembly."""
        return objects.registry.Assembly.get_by_uuid(self.context, id)

    def _context_from_trust_id(self, trust_id):
        cntx = context.RequestContext(trust_id=trust_id)
        kc = solum_keystoneclient.KeystoneClientV3(cntx)
        return kc.context

    def _verify_artifact(self, artifact, collab_url):
        # TODO(james_li): verify if the artifact is the one that gets triggered
        if collab_url is None:
            return True

        repo_token = artifact.get('repo_token')
        if repo_token is None:
            return False

        headers = {'Authorization': 'token ' + repo_token}
        try:
            resp, _ = httplib2.Http().request(collab_url, headers=headers)
            if resp['status'] == '204':
                return True
            elif resp['status'] == '404':
                raise exception.RequestForbidden(
                    reason="User %s not allowed to do rebuild" %
                           collab_url.split('/')[-1])
        except httplib2.HttpLib2Error as ex:
            LOG.info("Error in verifying collaborator %s" % ex)
        return False

    def trigger_workflow(self, trigger_id, commit_sha='',
                         status_url=None, collab_url=None):
        """Get trigger by trigger id and start git workflow associated."""
        # Note: self.context will be None at this point as this is a
        # non-authenticated request.
        db_obj = objects.registry.Assembly.get_by_trigger_id(None,
                                                             trigger_id)
        # get the trust context and authenticate it.
        try:
            self.context = self._context_from_trust_id(db_obj.trust_id)
        except exception.AuthorizationFailure as auth_ex:
            LOG.warn(auth_ex)
            return

        plan_obj = objects.registry.Plan.get_by_id(self.context,
                                                   db_obj.plan_id)

        artifacts = plan_obj.raw_content.get('artifacts', [])
        for arti in artifacts:
            if self._verify_artifact(arti, collab_url):
                self._build_artifact(assem=db_obj, artifact=arti,
                                     commit_sha=commit_sha,
                                     status_url=status_url,
                                     deploy_keys_ref=plan_obj.deploy_keys_uri)

    def update(self, id, data):
        """Modify a resource."""
        db_obj = objects.registry.Assembly.get_by_uuid(self.context, id)
        db_obj.update(data)
        db_obj.save(self.context)
        return db_obj

    def delete(self, id):
        """Delete a resource."""
        db_obj = objects.registry.Assembly.get_by_uuid(self.context, id)

        # delete the trust.
        ksc = solum_keystoneclient.KeystoneClientV3(self.context)
        ksc.delete_trust(db_obj.trust_id)

        db_obj.status = ASSEMBLY_STATES.DELETING
        db_obj.save(self.context)

        deploy_api.API(context=self.context).destroy(
            assem_id=db_obj.id)

    def create(self, data):
        """Create a new resource."""
        db_obj = objects.registry.Assembly()
        db_obj.update(data)
        db_obj.uuid = str(uuid.uuid4())
        db_obj.user_id = self.context.user
        db_obj.project_id = self.context.tenant
        db_obj.trigger_id = str(uuid.uuid4())
        db_obj.username = self.context.user_name

        # create the trust_id and store it.
        ksc = solum_keystoneclient.KeystoneClientV3(self.context)
        trust_context = ksc.create_trust_context()
        db_obj.trust_id = trust_context.trust_id

        db_obj.create(self.context)

        plan_obj = objects.registry.Plan.get_by_id(self.context,
                                                   db_obj.plan_id)

        artifacts = plan_obj.raw_content.get('artifacts', [])
        for arti in artifacts:
            self._build_artifact(assem=db_obj, artifact=arti,
                                 deploy_keys_ref=plan_obj.deploy_keys_uri)
        return db_obj

    def _build_artifact(self, assem, artifact, verb='build', commit_sha='',
                        status_url=None, deploy_keys_ref=None):

        # This is a tempory hack so we don't need the build client
        # in the requirments.
        image = objects.registry.Image()
        image.name = artifact['name']
        image.source_uri = artifact['content']['href']
        image.base_image_id = artifact.get('language_pack', 'auto')
        image.source_format = artifact.get('artifact_type',
                                           CONF.api.source_format)
        image.image_format = CONF.api.image_format
        image.uuid = str(uuid.uuid4())
        image.user_id = self.context.user
        image.project_id = self.context.tenant
        image.state = IMAGE_STATES.PENDING
        image.create(self.context)
        test_cmd = artifact.get('unittest_cmd')
        repo_token = artifact.get('repo_token')

        git_info = {
            'source_url': image.source_uri,
            'commit_sha': commit_sha,
            'repo_token': repo_token,
            'status_url': status_url
        }

        api.API(context=self.context).perform_action(
            verb=verb,
            build_id=image.id,
            git_info=git_info,
            name=image.name,
            base_image_id=image.base_image_id,
            source_format=image.source_format,
            image_format=image.image_format,
            assembly_id=assem.id,
            test_cmd=test_cmd,
            source_creds_ref=deploy_keys_ref)

    def get_all(self):
        """Return all assemblies, based on the query provided."""
        return objects.registry.AssemblyList.get_all(self.context)
