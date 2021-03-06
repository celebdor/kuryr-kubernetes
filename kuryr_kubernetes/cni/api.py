# Copyright (c) 2016 Mirantis, Inc.
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


import abc
import six
from six.moves import http_client as httplib
import traceback

import requests

from kuryr.lib._i18n import _
from os_vif.objects import base
from oslo_log import log as logging
from oslo_serialization import jsonutils

from kuryr_kubernetes.cni import utils
from kuryr_kubernetes import config
from kuryr_kubernetes import constants as k_const
from kuryr_kubernetes import exceptions as k_exc

LOG = logging.getLogger(__name__)


@six.add_metaclass(abc.ABCMeta)
class CNIPlugin(object):

    @abc.abstractmethod
    def add(self, params):
        raise NotImplementedError()

    @abc.abstractmethod
    def delete(self, params):
        raise NotImplementedError()


@six.add_metaclass(abc.ABCMeta)
class CNIRunner(object):
    # TODO(ivc): extend SUPPORTED_VERSIONS and format output based on
    # requested params.CNI_VERSION and/or params.config.cniVersion
    VERSION = '0.3.0'
    SUPPORTED_VERSIONS = ['0.3.0']

    @abc.abstractmethod
    def _add(self, params):
        raise NotImplementedError()

    @abc.abstractmethod
    def _delete(self, params):
        raise NotImplementedError()

    def _write_dict(self, fout, dct):
        output = {'cniVersion': self.VERSION}
        output.update(dct)
        LOG.debug("CNI output: %s", output)
        jsonutils.dump(output, fout, sort_keys=True)

    def _write_exception(self, fout, msg):
        self._write_dict(fout, {
            'msg': msg,
            'code': k_const.CNI_EXCEPTION_CODE,
            'details': traceback.format_exc(),
        })

    def _write_version(self, fout):
        self._write_dict(fout, {'supportedVersions': self.SUPPORTED_VERSIONS})

    @abc.abstractmethod
    def prepare_env(self, env, stdin):
        raise NotImplementedError()

    def run(self, env, fin, fout):
        try:
            # Prepare params according to calling Object
            params = self.prepare_env(env, fin)
            if env.get('CNI_COMMAND') == 'ADD':
                vif = self._add(params)
                self._write_dict(fout, vif)
            elif env.get('CNI_COMMAND') == 'DEL':
                self._delete(params)
            elif env.get('CNI_COMMAND') == 'VERSION':
                self._write_version(fout)
            else:
                raise k_exc.CNIError(_("unknown CNI_COMMAND: %s")
                                     % env['CNI_COMMAND'])
            return 0
        except Exception as ex:
            # LOG.exception
            self._write_exception(fout, str(ex))
            return 1

    def _vif_data(self, vif):
        result = {}
        nameservers = []

        for subnet in vif.network.subnets.objects:
            nameservers.extend(subnet.dns)

            ip = subnet.ips.objects[0].address
            cni_ip = result.setdefault("ip%s" % ip.version, {})
            cni_ip['ip'] = "%s/%s" % (ip, subnet.cidr.prefixlen)

            if subnet.gateway:
                cni_ip['gateway'] = str(subnet.gateway)

            if subnet.routes.objects:
                cni_ip['routes'] = [
                    {'dst': str(route.cidr), 'gw': str(route.gateway)}
                    for route in subnet.routes.objects]

        if nameservers:
            result['dns'] = {'nameservers': nameservers}
        return result


class CNIStandaloneRunner(CNIRunner):

    def __init__(self, plugin):
        self._plugin = plugin

    def _add(self, params):
        vif = self._plugin.add(params)
        return self._vif_data(vif)

    def _delete(self, params):
        self._plugin.delete(params)

    def prepare_env(self, env, stdin):
        return utils.CNIParameters(env, stdin)


class CNIDaemonizedRunner(CNIRunner):

    def _add(self, params):
        resp = self._make_request('addNetwork', params, httplib.ACCEPTED)
        vif = base.VersionedObject.obj_from_primitive(resp.json())
        return self._vif_data(vif)

    def _delete(self, params):
        self._make_request('delNetwork', params, httplib.NO_CONTENT)

    def prepare_env(self, env, stdin):
        cni_envs = {}
        cni_envs.update(
            {k: v for k, v in env.items() if k.startswith('CNI_')})
        cni_envs['config_kuryr'] = dict(stdin)
        return cni_envs

    def _make_request(self, path, cni_envs, expected_status=None):
        method = 'POST'

        address = config.CONF.cni_daemon.bind_address
        url = 'http://%s/%s' % (address, path)
        try:
            LOG.debug('Making request to CNI Daemon. %(method)s %(path)s\n'
                      '%(body)s',
                      {'method': method, 'path': url, 'body': cni_envs})
            resp = requests.post(url, json=cni_envs,
                                 headers={'Connection': 'close'})
        except requests.ConnectionError:
            LOG.exception('Looks like %s cannot be reached. Is kuryr-daemon '
                          'running?', address)
            raise
        LOG.debug('CNI Daemon returned "%(status)d %(reason)s".',
                  {'status': resp.status_code, 'reason': resp.reason})
        if expected_status and resp.status_code != expected_status:
            LOG.error('CNI daemon returned error "%(status)d %(reason)s".',
                      {'status': resp.status_code, 'reason': resp.reason})
            raise k_exc.CNIError('Got invalid status code from CNI daemon.')
        return resp
