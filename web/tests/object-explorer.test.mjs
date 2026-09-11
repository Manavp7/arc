import test from 'node:test';
import assert from 'node:assert/strict';
import {api} from '../src/lib/api.ts';
import {changeObjectFilter,emptyObjectFilters,nextObjectOffset,objectConfidence,objectExplorerApi,objectFilterOptions,objectFilterProblem,objectFramePath,objectObservations,objectPageLabel,objectQuery,objectTrackKey,reconcileObjectFilters,sameObjectFilters} from '../src/lib/object-explorer.ts';

const analysis=(id,created,classes,zones=[])=>({analysis_id:id,created_at:created,model:{name:'Detector',mode:'onnx'},classes,zones});
const catalog={videos:[{video_id:'one',title:'First clip',duration_s:12,analyses:[analysis('old','2026-01-01',['motion'],[{zone_id:'old-zone',name:'Old zone'}]),analysis('new','2026-01-03',['person','car'],[{zone_id:'new-zone',name:'Current zone'}])]},{video_id:'two',title:'Second clip',duration_s:20,analyses:[analysis('second','2026-01-02',['bus'],[{zone_id:'second-zone',name:'Second zone'}])]}],possibly_truncated:false,note:''};
const filters=extra=>({...emptyObjectFilters(),...extra});
const page=extra=>({results:[{},{}],count:120,limit:50,offset:50,possibly_truncated:true,scan_truncated:false,note:'',...extra});
const observation=(at,index,extra={})=>({at_s:at,frame_index:index,frame_url:`/api/review/videos/one/frames/old/${index}`,confidence:.8,bbox:[0,0,1,1],...extra});
const row=observations=>({video_id:'one',analysis_id:'old',track_id:'t1',first_s:0,last_s:10,observations});

test('cross-recording class choices use only newest completed catalog versions and version-specific zones',()=>{
  assert.deepEqual(objectFilterOptions(catalog,filters({})).classes,['bus','car','person']);
  assert.deepEqual(objectFilterOptions(catalog,filters({})).zones,[]);
  const current=objectFilterOptions(catalog,filters({video_id:'one'}));
  assert.equal(current.selected.analysis_id,'new');assert.deepEqual(current.classes,['car','person']);assert.equal(current.zones[0].zone_id,'new-zone');
  const old=objectFilterOptions(catalog,filters({video_id:'one',analysis_id:'old'}));
  assert.deepEqual(old.classes,['motion']);assert.equal(old.zones[0].zone_id,'old-zone');
});
test('changing a recording clears retained version, zone, class, and recording-relative times',()=>{
  const before=filters({video_id:'one',analysis_id:'old',zone_id:'old-zone',class_name:'motion',start_s:'3',end_s:'10',min_confidence:'.7'});
  assert.deepEqual(changeObjectFilter(before,'video_id','two'),filters({video_id:'two',min_confidence:'.7'}));
  assert.equal(before.video_id,'one');assert.equal(before.zone_id,'old-zone');
  assert.deepEqual(changeObjectFilter(before,'video_id','one'),before);
  assert.deepEqual(changeObjectFilter(before,'analysis_id','new'),{...before,analysis_id:'new',zone_id:'',class_name:''});
});
test('catalog refresh removes stale source and analysis filters without reusing unrelated zones',()=>{
  assert.deepEqual(reconcileObjectFilters(filters({video_id:'gone',analysis_id:'old',zone_id:'old-zone'}),catalog),emptyObjectFilters());
  const result=reconcileObjectFilters(filters({video_id:'one',analysis_id:'missing',zone_id:'old-zone',class_name:'motion',start_s:'2'}),catalog);
  assert.equal(result.analysis_id,'');assert.equal(result.zone_id,'');assert.equal(result.class_name,'');assert.equal(result.start_s,'2');
  assert.equal(reconcileObjectFilters(filters({video_id:'one',analysis_id:'new',zone_id:'second-zone',class_name:'bus'}),catalog).zone_id,'');
  assert.deepEqual(reconcileObjectFilters(filters({video_id:'one',analysis_id:'old',zone_id:'old-zone',class_name:'motion'}),catalog),filters({video_id:'one',analysis_id:'old',zone_id:'old-zone',class_name:'motion'}));
});
test('query validation rejects unsupported scopes and invalid confidence or time windows',()=>{
  assert.match(objectFilterProblem(filters({analysis_id:'old'})),/recording/);
  assert.match(objectFilterProblem(filters({zone_id:'zone'})),/recording/);
  for(const value of ['NaN','Infinity','-.01','1.001'])assert.match(objectFilterProblem(filters({min_confidence:value})),/confidence/);
  for(const value of ['NaN','Infinity','-1'])assert.match(objectFilterProblem(filters({start_s:value})),/seconds/);
  assert.match(objectFilterProblem(filters({start_s:'5',end_s:'4'})),/end/);
  assert.match(objectFilterProblem(filters({start_s:'13'}),12),/duration/);
  assert.match(objectFilterProblem(filters({start_s:'180.001'})),/180-second/);
  assert.match(objectFilterProblem(filters({end_s:'181'})),/180-second/);
  assert.equal(objectFilterProblem(filters({end_s:'180'})),null);
  assert.equal(objectFilterProblem(filters({min_confidence:'0',start_s:'0',end_s:'12'}),12),null);
  assert.equal(objectFilterProblem(filters({start_s:'3',end_s:'3'})),null);
  assert.throws(()=>objectQuery(filters({end_s:'bad'})),/seconds/);
});
test('queries omit empty values but preserve exact version, zero confidence and zero clip offsets',()=>{
  const query=new URLSearchParams(objectQuery(filters({video_id:'one/a',analysis_id:'old&v',min_confidence:'0',start_s:'0',end_s:'4.5'}),50));
  assert.equal(query.get('video_id'),'one/a');assert.equal(query.get('analysis_id'),'old&v');assert.equal(query.get('min_confidence'),'0');assert.equal(query.get('start_s'),'0');assert.equal(query.get('offset'),'50');assert.equal(query.get('limit'),'50');assert.equal(query.has('zone_id'),false);assert.equal(query.has('class_name'),false);
  assert.equal(new URLSearchParams(objectQuery(emptyObjectFilters(),-50)).get('offset'),'0');
  assert.equal(new URLSearchParams(objectQuery(emptyObjectFilters(),Infinity)).get('offset'),'0');
  assert.equal(new URLSearchParams(objectQuery(emptyObjectFilters(),20050)).get('offset'),'20050');
  assert.equal(new URLSearchParams(objectQuery(emptyObjectFilters(),50000)).get('offset'),'50000');
  assert.equal(new URLSearchParams(objectQuery(emptyObjectFilters(),75000)).get('offset'),'50000');
});
test('pagination uses total count and offset while explicitly qualifying incomplete scans',()=>{
  assert.equal(nextObjectOffset(page({})),100);
  assert.equal(nextObjectOffset(page({offset:100,count:102})),null);
  assert.equal(nextObjectOffset(page({offset:20000,count:21000})),20050);
  assert.equal(nextObjectOffset(page({results:[]})),null);
  assert.equal(objectPageLabel(page({})),'51–52 of 120 matching tracks');
  assert.equal(objectPageLabel(page({scan_truncated:true})),'51–52 of at least 120 matching tracks');
  assert.equal(objectPageLabel(page({offset:0,results:[{}],count:1})),'1–1 of 1 matching track');
  assert.equal(objectPageLabel(page({offset:0,results:[]})),'No matching tracks');
  assert.equal(objectPageLabel(page({results:[]})),'No tracks on this page');
});
test('all matching tracks remain reachable beyond 20,000 with a final partial page and the scan boundary',()=>{
  const full=Array.from({length:50},()=>({}));
  const afterOldCap=page({offset:20000,count:20051,results:full});
  assert.equal(nextObjectOffset(afterOldCap),20050);
  const finalPartial=page({offset:20050,count:20051,results:[{}]});
  assert.equal(nextObjectOffset(finalPartial),null);
  assert.equal(objectPageLabel(finalPartial),'20051–20051 of 20051 matching tracks');
  assert.equal(nextObjectOffset(page({offset:49900,count:50000,results:full})),49950);
  assert.equal(nextObjectOffset(page({offset:49950,count:50000,results:full})),null);
  assert.equal(nextObjectOffset(page({offset:50000,count:50100,results:full})),null);
});
test('sample navigation sorts only returned observations and never synthesizes first or last times',()=>{
  const first=observation(2,4),last=observation(8,16);
  const result=objectObservations(row([last,observation(NaN,12),first,observation(-1,0),observation(3,-4),first]));
  assert.deepEqual(result,[first,last]);assert.equal(result[0].at_s,2);assert.equal(result.at(-1).at_s,8);
  assert.deepEqual(objectObservations(row([])),[]);
});
test('thumbnails pin the recording, retained version and frame index and reject other paths',()=>{
  const record=row([]);
  assert.equal(objectFramePath(record,observation(2,4)),'/api/review/videos/one/frames/old/4');
  for(const frame_url of ['https://example.test/frame.jpg','/api/review/videos/two/frames/old/4','/api/review/videos/one/frames/new/4','/api/review/videos/one/frames/old/5',''])assert.equal(objectFramePath(record,observation(2,4,{frame_url})),null);
  assert.equal(objectFramePath(record,undefined),null);assert.equal(objectFramePath(record,observation(2,-1)),null);
});
test('confidence and filter comparisons preserve unknown values and changed zero-valued filters',()=>{
  assert.equal(objectConfidence(null),'Not measured');assert.equal(objectConfidence(NaN),'Not measured');assert.equal(objectConfidence(0),'0%');assert.equal(objectConfidence(.865),'87%');
  assert.equal(sameObjectFilters(filters({}),filters({})),true);assert.equal(sameObjectFilters(filters({}),filters({min_confidence:'0'})),false);
});
test('track result identity includes the detected class as well as its recording and analysis',()=>{
  const first={video_id:'one',analysis_id:'new',track_id:'t1',class_name:'person'};
  for(const changes of [{video_id:'two'},{analysis_id:'old'},{track_id:'t2'},{class_name:'car'}])assert.notEqual(objectTrackKey(first),objectTrackKey({...first,...changes}));
  assert.equal(objectTrackKey(first),objectTrackKey({...first}));
});
test('API reads carry abort signals and retained filter values without issuing writes',async()=>{
  const original=api.request,calls=[],controller=new AbortController();api.request=async(path,options)=>{calls.push([path,options]);return {};};
  try{await objectExplorerApi.catalog(controller.signal);await objectExplorerApi.search(filters({video_id:'one',analysis_id:'old',zone_id:'old-zone',start_s:'0',end_s:'12'}),50,controller.signal);
    assert.equal(calls[0][0],'/review/objects/catalog');assert.equal(calls[0][1].signal,controller.signal);assert.equal(calls[1][1].signal,controller.signal);assert.equal(calls[1][1].method,undefined);
    const url=new URL(calls[1][0],'https://local.invalid');assert.equal(url.pathname,'/review/objects');assert.equal(url.searchParams.get('analysis_id'),'old');assert.equal(url.searchParams.get('offset'),'50');
  }finally{api.request=original;}
});
