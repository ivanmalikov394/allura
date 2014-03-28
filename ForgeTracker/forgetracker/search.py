#       Licensed to the Apache Software Foundation (ASF) under one
#       or more contributor license agreements.  See the NOTICE file
#       distributed with this work for additional information
#       regarding copyright ownership.  The ASF licenses this file
#       to you under the Apache License, Version 2.0 (the
#       "License"); you may not use this file except in compliance
#       with the License.  You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#       Unless required by applicable law or agreed to in writing,
#       software distributed under the License is distributed on an
#       "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#       KIND, either express or implied.  See the License for the
#       specific language governing permissions and limitations
#       under the License.
from pylons import tmpl_context as c

from allura.lib.search import search


FACET_PARAMS = {
    'facet': 'true',
    'facet.field': ['_milestone_s', 'status_s', 'assigned_to_s', 'reported_by_s'],
    'facet.limit': -1,
    'facet.sort': 'index',
    'facet.mincount': 1,
}


def query_filter_choices(arg=None, final_result=None):
    params = {
        'short_timeout': True,
        'fq': [
            'project_id_s:%s' % c.project._id,
            'mount_point_s:%s' % c.app.config.options.mount_point,
            'type_s:Ticket',
            ],
        'rows': 0,
    }
    params.update(FACET_PARAMS)
    result = search(arg, **params)
    return get_facets(result, final_result)


def get_facets(solr_hit, final_result=None):
    result = {}
    if solr_hit is not None:
        for facet_name, values in solr_hit.facets['facet_fields'].iteritems():
            field_name = facet_name.rsplit('_s', 1)[0]
            values = [(values[i], values[i+1]) for i in xrange(0, len(values), 2)]
            if final_result:
                available_values = []
                for ticket in final_result:
                    solr_data = ticket.index()
                    if facet_name in solr_data:
                        available_values.append(solr_data[facet_name])

                values = filter(lambda v: v[0] in available_values, values)

            result[field_name] = values
    return result
