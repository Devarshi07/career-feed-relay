"""Read official career feeds and relay snapshots to owner-configured endpoints."""
import datetime
import email.utils
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
QUERIES=['software','machine learning','infrastructure','inference','AI engineer','ML','platform']
CVS='https://cvshealth.wd1.myworkdayjobs.com'
CVS_QUERIES=['software','machine learning','AI engineer','platform engineer','infrastructure','site reliability']
# Ashby lists every department at once; only technical titles are relayed to keep receiver traffic small.
TECHNICAL=re.compile(r'\b(engineer\w*|developer|scientist|research\w*|software|machine learning|ml|ai|inference|platform|infrastructure|mlops|sre)\b',re.I)
LIMIT=8000000
UA='CareerFeedRelay/1.0 public-career-feed-monitor'
class FeedError(Exception):
    def __init__(self,message,retry_after=300): super().__init__(message);self.retry_after=retry_after
def request(url,body=None,key=None):
    headers={'User-Agent':UA,'Accept':'application/json','Accept-Encoding':'identity'}
    if body is not None: headers['Content-Type']='application/json'
    if key: headers['Authorization']='Bearer '+key
    req=urllib.request.Request(url,json.dumps(body).encode() if body is not None else None,headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req,timeout=30) as response: raw=response.read(LIMIT+1)
            if len(raw)>LIMIT: raise FeedError('Response exceeded '+str(LIMIT//1000000)+' MB; not processed')
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            if urllib.parse.urlparse(url).path=='/api/feed-relay' and error.code==400:
                try: message=json.loads(error.read()).get('error','Invalid receiver payload')
                except (ValueError,TypeError): message='Invalid receiver payload'
                raise FeedError('Receiver '+str((body or {}).get('phase'))+': '+str(message)[:120]) from None
            if error.code in (403,429):
                raw=error.headers.get('Retry-After','')
                try: delay=int(raw)
                except ValueError:
                    try: delay=int(email.utils.parsedate_to_datetime(raw).timestamp()-time.time())
                    # Without Retry-After, a 429 is retried on the next five-minute run; a 403 waits 15 minutes.
                    except (ValueError,TypeError): delay=240 if error.code==429 else 900
                # Name the rejected public endpoint; receiver addresses stay out of public logs.
                endpoint=urllib.parse.urlparse(url)
                where='Receiver' if endpoint.path=='/api/feed-relay' else endpoint.hostname+endpoint.path
                raise FeedError(where+' returned HTTP '+str(error.code)+(' (Retry-After '+raw[:40]+')' if raw else ''),max(60,delay)) from None
            if error.code==404: raise FeedError('Career endpoint returned HTTP 404') from None
            if error.code not in (409,500,502,503,504) or attempt==2: raise FeedError('Request failed: HTTP '+str(error.code)) from None
        except (TimeoutError,urllib.error.URLError):
            if attempt==2: raise FeedError('Network request failed; a later schedule can retry') from None
        time.sleep(3*(attempt+1))
def wayfair(state):
    jobs={}
    for query in QUERIES:
        url='https://www.wayfair.com/a/careers/careers/job_search_data?'+urllib.parse.urlencode({'keywords':query})
        data=request(url)
        if not isinstance(data.get('jobListData'),list): raise FeedError('Career feed schema changed')
        for row in data['jobListData']:
            if row.get('isActive') is False or row.get('isHidden') or row.get('isReferralOnly'): continue
            location=row.get('location') or ''
            if isinstance(location,dict): location=', '.join(v for v in location.values() if isinstance(v,str))
            url=row.get('structuredDataApplyLink') or row.get('applyLink')
            if not row.get('id') or not row.get('title') or not url: raise FeedError('Incomplete job record')
            jobs[str(row['id'])]={'id':str(row['id']),'title':row['title'],'url':url,'location':location,'description':row.get('description') or '', 'version':str(row.get('lastUpdatedDate') or ''),'posted':row.get('createdDate') or ''}
        time.sleep(0.75)
    return list(jobs.values()),0
def microsoft(state):
    base='https://apply.careers.microsoft.com/api/pcsx'
    jobs={};cursor=max(20,int(state.get('cursor') or 20));next_cursor=cursor
    other=['machine learning','inference','infrastructure','AI engineer'][int(time.time()/300)%4]
    for query,offset in [('software',0),('software',10),(other,0),('software',cursor)]:
        params={'domain':'microsoft.com','query':query,'start':offset,'sort_by':'timestamp'}
        if os.environ.get('MICROSOFT_LOCATION'):params['location']=os.environ['MICROSOFT_LOCATION']
        envelope=request(base+'/search?'+urllib.parse.urlencode(params));data=envelope.get('data') or {}
        if envelope.get('status')!=200 or not isinstance(data.get('positions'),list): raise FeedError('Microsoft career search schema changed')
        rows=data['positions'];total=int(data['count'])
        if total>offset and not rows: raise FeedError('Microsoft snapshot incomplete')
        for row in rows:
            jid=str(row['id']);location='; '.join(row.get('locations') or []);title=row['name']
            jobs[jid]={'id':jid,'title':title,'url':'https://apply.careers.microsoft.com/careers/job/'+jid,'location':location,'description':'','version':'|'.join([title,location,str(row.get('postedTs') or '')]),'posted':datetime.datetime.fromtimestamp(row['postedTs'],datetime.timezone.utc).isoformat() if row.get('postedTs') else ''}
        if offset==cursor:next_cursor=20 if offset+len(rows)>=total else offset+len(rows)
        time.sleep(0.75)
    return list(jobs.values()),next_cursor
def microsoft_detail(job):
    detail=request('https://apply.careers.microsoft.com/api/pcsx/position_details?'+urllib.parse.urlencode({'domain':'microsoft.com','position_id':job['id']}))
    data=detail.get('data') or {}
    if detail.get('status')!=200 or not data.get('jobDescription'):raise FeedError('Microsoft full description unavailable')
    return {**job,'description':data['jobDescription'],'location':'; '.join(data.get('locations') or []) or job['location']}
def cvs(state):
    jobs={}
    for query in CVS_QUERIES:
        data=request(CVS+'/wday/cxs/cvshealth/CVS_Health_Careers/jobs',{'appliedFacets':{},'limit':20,'offset':0,'searchText':query})
        if not isinstance(data.get('jobPostings'),list): raise FeedError('CVS career search schema changed')
        # Workday lists newest first, so the first page of each search holds new postings.
        for row in data['jobPostings']:
            path=row.get('externalPath') or '';jid=(row.get('bulletFields') or [''])[0] or path.rsplit('_',1)[-1]
            if not row.get('title') or not path.startswith('/job/') or not re.fullmatch(r'[A-Za-z0-9-]{1,40}',jid): raise FeedError('Incomplete job record')
            jobs[jid]={'id':jid,'title':row['title'],'url':CVS+'/CVS_Health_Careers'+path,'location':row.get('locationsText') or '','description':'','version':row['title']+'|'+path,'posted':''}
        time.sleep(0.75)
    return list(jobs.values()),0
def cvs_detail(job):
    path=urllib.parse.urlparse(job['url']).path.removeprefix('/CVS_Health_Careers')
    data=request(CVS+'/wday/cxs/cvshealth/CVS_Health_Careers'+path).get('jobPostingInfo') or {}
    if not data.get('jobDescription'): raise FeedError('CVS full description unavailable')
    places=[data.get('location'),*(data.get('additionalLocations') or []),(data.get('country') or {}).get('descriptor')]
    return {**job,'description':data['jobDescription'],'location':'; '.join(p for p in places if p) or job['location'],'posted':data.get('startDate') or job.get('posted') or ''}
def cerebras(state):
    data=request('https://api.ashbyhq.com/posting-api/job-board/cerebras')
    if not isinstance(data.get('jobs'),list): raise FeedError('Cerebras job board schema changed')
    jobs=[]
    for row in data['jobs']:
        title=(row.get('title') or '').strip()
        if row.get('isListed') is False or not TECHNICAL.search(title): continue
        places=[row.get('location'),*[s.get('location') for s in row.get('secondaryLocations') or []],'Remote' if row.get('workplaceType')=='Remote' else None,((row.get('address') or {}).get('postalAddress') or {}).get('addressCountry')]
        location='; '.join(dict.fromkeys(p for p in places if p))
        if not row.get('id') or not title or not row.get('jobUrl'): raise FeedError('Incomplete job record')
        description=row.get('descriptionHtml') or row.get('descriptionPlain') or ''
        jobs.append({'id':row['id'],'title':title,'url':row['jobUrl'],'location':location,'description':description,'version':hashlib.sha256('|'.join([title,location,description]).encode()).hexdigest()[:24],'posted':row.get('publishedAt') or ''})
    return jobs,0
def deliver(endpoint,key):
    # ntfy.sh limits senders per IP and the receiver's shared cloud addresses exhaust it, so queued alerts are published here.
    # Message contents come from the receiver and are never printed.
    box=request(endpoint,{'phase':'outbox'},key);results=[]
    for item in box.get('messages') or []:
        try:
            reply=request('https://ntfy.sh',item['ntfy'])
            results.append({'event_id':item['event_id'],'ok':True} if reply.get('id') else {'event_id':item['event_id'],'ok':False,'error':'ntfy did not confirm acceptance'})
        except FeedError as error: results.append({'event_id':item['event_id'],'ok':False,'error':str(error)[:200],'retry_after':error.retry_after})
    if results: request(endpoint,{'phase':'delivered','owner':box.get('owner',''),'results':results},key)
    return sum(1 for r in results if r['ok']),len(results)
# Collectors without a description step already return full descriptions.
SOURCES=[('wayfair',wayfair,None),('microsoft',microsoft,microsoft_detail),('cvs',cvs,cvs_detail),('cerebras',cerebras,None)]
def main():
    failed=False;dry=os.environ.get('RELAY_DRY_RUN')=='true';scheduled=os.environ.get('RELAY_SCHEDULED')=='true'
    only=set(filter(None,os.environ.get('RELAY_SOURCES','').split(',')))
    for source,collector,describe in SOURCES:
        if only and source not in only: continue
        scan=''
        endpoint=os.environ.get(source.upper()+'_INGEST_URL','');key=os.environ.get('CAREER_RELAY_KEY','')
        if not dry and not endpoint: print(source+': not configured; skipped.');continue
        try:
            if not dry:
                parsed=urllib.parse.urlparse(endpoint)
                if parsed.scheme!='https' or parsed.path!='/api/feed-relay':raise FeedError('Configure an HTTPS /api/feed-relay endpoint')
            state={} if dry else request(endpoint,{'phase':'status'},key)
            if state.get('next_allowed',0)>time.time()*1000: print(source+': checked moments ago or cooling down; skipped.');continue
            jobs,next_cursor=collector(state)
            if not jobs:raise FeedError('Unexpected empty feed requires review; not marked successful')
            if dry:
                sample=describe(jobs[0]) if describe else jobs[0]
                print(f'{source}: public feed readable; {len(jobs)} jobs; sample "{sample["title"]}" at {sample["location"][:80]} with {len(sample["description"])} description chars; nothing relayed.');continue
            scan=str(uuid.uuid4())
            result=request(endpoint,{'phase':'begin','run_id':scan,'expected':len(jobs),'scheduled':scheduled},key)
            for offset in range(result.get('received',0),len(jobs),2):request(endpoint,{'phase':'chunk','run_id':scan,'offset':offset,'jobs':jobs[offset:offset+2]},key)
            # The private receiver decides which descriptions are needed; no role rules live here.
            if describe:
                queue=request(endpoint,{'phase':'status'},key).get('pending',[])
                enriched=[]
                for job in queue:
                    time.sleep(3)
                    enriched.append(describe(job))
                for offset in range(0,len(enriched),2):request(endpoint,{'phase':'details','run_id':scan,'jobs':enriched[offset:offset+2]},key)
            result=request(endpoint,{'phase':'finish','run_id':scan,'next_cursor':next_cursor},key)
            print(f'{source}: complete snapshot relayed; {result["received"]} jobs; recurring mode: {result["scheduled"]}.')
        except Exception as error:
            failed=True;message=str(error) if isinstance(error,FeedError) else 'Feed processing failed: '+type(error).__name__
            print(source+': '+message)
            if not dry:
                try:request(endpoint,{'phase':'failure','run_id':scan,'error':message,'retry_after':getattr(error,'retry_after',300),'scheduled':scheduled},key)
                except Exception:print(source+': could not report the failure to the receiver.')
    # Alerts go out even when a company's check was skipped or failed; undelivered ones retry on a later run.
    for source,_,_ in SOURCES if not dry else []:
        endpoint=os.environ.get(source.upper()+'_INGEST_URL','')
        if (only and source not in only) or not endpoint: continue
        try:
            sent,total=deliver(endpoint,os.environ.get('CAREER_RELAY_KEY',''))
            if total: print(f'{source}: {sent} of {total} queued alerts published.')
        except Exception as error: print(source+': alert delivery failed: '+(str(error) if isinstance(error,FeedError) else type(error).__name__))
    raise SystemExit(int(failed))
if __name__=='__main__':main()
