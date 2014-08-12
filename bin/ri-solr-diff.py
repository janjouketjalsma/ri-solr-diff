#!/usr/sbin/env python

import dateutil.parser
import time
import requests
import argparse
import logging
import json
logging.basicConfig(format='%(asctime)s - %(message)s', datefmt='%s', level=logging.INFO)

# requests is kind of noisy by default... Let's shut it up.
logging.getLogger('requests').setLevel(logging.WARNING)

parser = argparse.ArgumentParser(
  description='Identify and resolve differences between a Fedora Resource and Solr index.',
  epilog='Exit code will be "0" if everything was up-to-date. If documents were updated, the exit code will be "1" (though may also be "1" due to runtime errors).'
)
# Connection arguments
parser.add_argument('--ri', default="http://localhost:8080/fedora/risearch", help='URL of the resource index at the host. (default: %(default)s)')
parser.add_argument('--ri-user', default='fedoraAdmin', help='Username to communicate with resource index, if necessary. (default: %(default)s)')
parser.add_argument('--ri-pass', default='islandora', help='Password to communicate with resource index, if necessary. (default: %(default)s)')
parser.add_argument('--solr', default="http://localhost:8080/solr", help='Hostname/IP of the Solr index. (default: %(default)s)')
parser.add_argument('--solr-last-modified-field', default='fgs_lastModifiedDate_dt', help='The Solr field storing the last modified date of each object. (default: %(default)s)')
parser.add_argument('--gsearch', default="http://localhost:8080/fedoragsearch/rest", help="Hostname/IP of GSearch (default: %(default)s)")
parser.add_argument('--gsearch-user', default='fedoraAdmin', help='Username to communicate with GSearch servelet, if necessary. (default: %(default)s)')
parser.add_argument('--gsearch-pass', default='islandora', help='Password to communicate with GSearch servelet, if necessary. (default: %(default)s)')
parser.add_argument('--query-limit', default=10000, type=int, help='The number of results which will be fetched from the RI and Solr at a time. (default: %(default)s)')

# Application switches
group = parser.add_mutually_exclusive_group(required=True)
group.add_argument('--all', help='Compare all objects.', action='store_true')
group.add_argument('--last-n-days', type=int, help='Compare objects modified in the last n days.')
group.add_argument('--last-n-seconds', type=int, help='Compare objects modified in the last n seconds.')
group.add_argument('--since', type=int, help='Compare objects modified since the given Unix timestamp.')

log_group = parser.add_mutually_exclusive_group()
log_group.add_argument('--verbose', '-v', default=0, action='count', help='Adjust verbosity of output. More times == more verbose.')
log_group.add_argument('--quiet', '-q', default=0, action='count', help='Adjust verbosity of output. More times == less verbose.')

class ri_generator:
    def __init__(self, url, user=None, password=None, start=None, limit=10000):
        self.url = url
        self.user = user
        self.password = password
        self.start = start
        self.limit = limit

    def __iter__(self):
        replacements = {
          'filter': ''
        }
        if self.start is not None:
            replacements['filter'] = 'FILTER(?timestamp >= "%s"^^<http://www.w3.org/2001/XMLSchema#dateTime>)' % (self.start)

        query = '''
SELECT ?obj ?timestamp
FROM <#ri>
WHERE {
  ?obj <fedora-model:hasModel> <info:fedora/fedora-system:FedoraObject-3.0> ;
       <fedora-model:state> <fedora-model:Active> ;
       <fedora-view:lastModifiedDate> ?timestamp .
  OPTIONAL {
    ?obj <fedora-view:disseminates> ?exclude .
    {
      ?exclude <fedora-view:disseminationType> <info:fedora/*/DS-COMPOSITE-MODEL> .
    } UNION {
      ?exclude <fedora-view:disseminationType> <info:fedora/*/METHODMAP> .
    }
  }
  FILTER(!bound(?exclude))
  %(filter)s
}
ORDER BY ?timestamp ?obj
'''
        data = {
            'type': 'tuples',
            'format': 'json',
            'lang': 'sparql',
            'query': query % replacements,
            'limit': self.limit
        }
        s = requests.Session()
        s.auth = (self.user, self.password)
        r = s.post(self.url, data=data)

        while r.status_code == requests.codes.ok:
            # XXX: Seems to be some weird encoding issue preventing r.json()
            # from working?
            query_result = json.loads(r.text)

            if len(query_result['results']) == 0:
              break

            for result in query_result['results']:
                yield (result['obj'].split('info:fedora/')[1], dateutil.parser.parse(result['timestamp']))

            # Grab the last timestamp, to start from it.
            self.start = query_result['results'][-1]['timestamp']

            replacements['filter'] = 'FILTER(?timestamp > "%s"^^<http://www.w3.org/2001/XMLSchema#dateTime>)' % (self.start)
            data['query'] = query % replacements
            r = s.post(self.url, data=data)

class solr_generator:
    def __init__(self, url, field, start=None, limit=10000):
        self.base_url = url
        self.url = "%s/select" % url
        self.field = field
        self.start = start
        self.limit = limit

    def __iter__(self):
        params = {
          'q': '*:*',
          'sort': '%s asc, PID asc' % self.field,
          'wt': 'json',
          'fl': 'PID %s' % self.field,
          'rows': self.limit
        }
        if self.start is not None:
            params['fq'] = ["%s:{%s TO *}" % (self.field, self.start)]

        r = requests.post(self.url, data=params)

        while r.status_code == requests.codes.ok:
            # XXX: Seems to be some weird encoding issue preventing r.json()
            # from working?
            query_results = json.loads(r.text)

            if query_results['response']['numFound'] == 0:
              break

            for result in query_results['response']['docs']:
                yield (result['PID'], dateutil.parser.parse(result[self.field]))

            # Grab the last timestamp, to start from it.
            self.start = query_results['response']['docs'][-1][self.field]

            params['fq'] = ["%s:{%s TO *}" % (self.field, self.start)]
            r = requests.post(self.url, data=params)

class gsearch:
    def __init__(self, url, user, password):
        self.url = url
        self.user = user
        self.password = password
        self.session = requests.Session()
        self.session.auth = (self.user, self.password)
        self.updated = False

    def update_pid(self, pid):
        if not self.updated:
          self.updated = True

        data = {
          'operation': 'updateIndex',
          'action': 'fromPid',
          'value': pid
        }
        logging.debug('Attempting to update %s...' % pid)
        r = self.session.post(self.url, data=data)
        if r.status_code == requests.codes.ok:
            logging.debug('Updated %s' % pid)
            logging.info(pid)
        else:
            logging.debug('Failed to update %s?' % pid)

if __name__ == '__main__':
    args = parser.parse_args()
    logging.getLogger().setLevel(logging.INFO + (-args.verbose + args.quiet) * 10)

    start = None
    timestamp = 0
    if args.last_n_days:
        timestamp = time.time() - (24 * 3600 * args.last_n_days)
    elif args.last_n_seconds:
        timestamp = time.time() - args.last_n_seconds
    elif args.since:
        timestamp = args.since

    if not args.all:
        # Use "timestamp" to set "start"
        start = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(timestamp))

    ri = iter(ri_generator(args.ri, args.ri_user, args.ri_pass, start=start, limit=args.query_limit))
    solr = iter(solr_generator(args.solr, args.solr_last_modified_field, start=start, limit=args.query_limit))
    gsearch = gsearch(args.gsearch, args.gsearch_user, args.gsearch_pass)

    try:
        ri_result = ri.next()
        solr_result = solr.next()

        while ri_result and solr_result:
            ri_pid, ri_time = ri_result
            solr_pid, solr_time = solr_result

            if ri_time < solr_time:
                logging.debug('RI older, update %s.' % ri_pid)
                gsearch.update_pid(ri_pid)
                ri_result = ri.next()
            elif solr_time < ri_time:
                logging.debug('Solr older, update %s.' % solr_pid)
                gsearch.update_pid(solr_pid)
                solr_result = solr.next()
            else:
                # Hit stuff with the same time... Start comparing PIDs.
                if ri_pid < solr_pid:
                    logging.debug('RI pid, update %s.' % ri_pid)
                    gsearch.update_pid(ri_pid)
                    ri_result = ri.next()
                elif solr_pid < ri_pid:
                    logging.debug('Solr pid, update %s.' % solr_pid)
                    gsearch.update_pid(solr_pid)
                    solr_result = solr.next()
                else:
                  # Same PID, same time, up-to-date... Skip!
                    logging.debug('Docs appear equal for %s.' % ri_pid)
                    ri_result = ri.next()
                    solr_result = solr.next()
    except StopIteration:
        pass

    for ri_pid, ri_time in ri:
        #Stuff left over from RI... Reindex.
        logger.debug('RI, leftover: %s' % ri_pid)
        gsearch.update_pid(ri_pid)

    for solr_pid, solr_time in solr:
        # Stuff left over from Solr. Recently indexed and purged, but index
        # failed to update... Should probably delete...  Let's just try
        # reindexing.
        logger.debug('Solr, leftover: %s' % ri_pid)
        gsearch.update_pid(solr_pid)

    if gsearch.updated:
        exit(1)
    else:
        exit(0)
