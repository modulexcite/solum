# -*- coding: utf-8 -*-
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

from solum.api.controllers import common_types
from solum.api.controllers.v1.datamodel import types as api_types


class Extensions(api_types.Base):
    """CAMP v1.1 extensions resource model."""

    extension_links = [common_types.Link]
    """This attribute contains Links to extension resources that contain
     information about the extensions supported by this Platform."""

    def __init__(self, **kwds):
        super(Extensions, self).__init__(**kwds)
