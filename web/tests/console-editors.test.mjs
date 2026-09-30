import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import {Window} from 'happy-dom';
const window = new Window({url:'http://localhost/'});
for (const name of ['window','document','HTMLElement','HTMLInputElement','HTMLSelectElement','HTMLTextAreaElement','Event','MouseEvent','BeforeUnloadEvent','PopStateEvent','history','localStorage','sessionStorage']) Object.defineProperty(globalThis,name,{configurable:true,value:name === 'window' ? window : window[name]});
Object.defineProperty(globalThis,'navigator',{configurable:true,value:window.navigator});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const originalFetch=globalThis.fetch;
let fetchHandler=(...args)=>originalFetch(...args);
globalThis.fetch=(...args)=>fetchHandler(...args);
const {createElement:h,act} = await import('react');
const {createRoot} = await import('react-dom/client');
const {SitePanel} = await import('../src/components/SitePanel.tsx');
const {CameraSetupPanel} = await import('../src/components/CameraSetupPanel.tsx');
const {EvidencePackagePanel} = await import('../src/components/EvidencePackagePanel.tsx');
const {MissionControlPanel} = await import('../src/components/MissionControlPanel.tsx');
const {CalibrationPublicationControls} = await import('../src/components/CalibrationPublicationControls.tsx');
const {api} = await import('../src/lib/api.ts');
const {calibrationApi} = await import('../src/lib/calibration-publication.ts');
const session = await import('../src/lib/session.ts');
const token = `x.${Buffer.from(JSON.stringify({sub:'reviewer',tenant:'test',roles:['admin'],clearance:3,exp:Math.floor(Date.now()/1000)+3600})).toString('base64url')}.x`;
fetchHandler=async url => String(url).startsWith('/auth/config') ? new Response(JSON.stringify({mode:'dev',required:true,oidc:null})) : String(url).startsWith('/auth/dev/token') ? new Response(JSON.stringify({access_token:token})) : new Response(new Uint8Array([0]),{headers:{'content-type':'video/mp4'}});
await session.signIn('admin');
after(async()=>{globalThis.fetch=originalFetch;await window.happyDOM.close();});
async function mount(t,element) {
  const container=document.createElement('div');document.body.append(container);const root=createRoot(container);
  await act(async()=>{root.render(element);});
  t.after(async()=>{await act(async()=>root.unmount());container.remove();});
  return {container,root};
}
async function change(input,value) {
  assert.ok(input,'input exists');
  const proto=input.tagName==='SELECT'?window.HTMLSelectElement.prototype:input.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;
  await act(async()=>{Object.getOwnPropertyDescriptor(proto,'value').set.call(input,value);input.dispatchEvent(new window.Event(input.tagName==='SELECT'?'change':'input',{bubbles:true}));});
}
const button=(container,text)=>[...container.querySelectorAll('button')].find(node=>node.textContent.trim()===text);
const field=(container,label)=>[...container.querySelectorAll('label')].find(node=>node.textContent.trim().startsWith(label))?.querySelector('input,textarea,select');
async function click(node){assert.ok(node,'button exists');await act(async()=>node.click());}
function mockApi(t,replacements){const old={};for(const [name,value]of Object.entries(replacements)){old[name]=api[name];api[name]=value;}t.after(()=>Object.assign(api,old));}
const site=id=>({site_id:id,name:`Site ${id}`,notes:'',revision:1,zones:[],cameras:[]});

test('site selection reports exact record and save/discard releases reload/navigation protection',async t=>{
  const sites=[site('a'),site('b')],selection=[],dirty=[],writes=[];
  mockApi(t,{request:async(path,init)=>{if(path==='/sites')return{sites};if(init?.method==='PUT'){writes.push(JSON.parse(init.body));return{...sites[0],...writes.at(-1),revision:2};}throw Error(path);}});
  const {container}=await mount(t,h(SitePanel,{onSelectSite:id=>selection.push(id),onDirtyChange:value=>dirty.push(value)}));
  assert.equal(selection.at(-1),'a');
  await change(field(container,'Site name'),'Revised site');assert.equal(dirty.at(-1),true);
  const unload=new window.Event('beforeunload',{cancelable:true});window.dispatchEvent(unload);assert.equal(unload.defaultPrevented,true);
  assert.equal(container.querySelector('select[aria-label="Site"]').disabled,true);
  await click(button(container,'Save layout'));assert.equal(writes[0].name,'Revised site');assert.equal(dirty.at(-1),false);
  await change(container.querySelector('select[aria-label="Site"]'),'b');assert.equal(selection.at(-1),'b');
  await change(field(container,'Planning notes'),'unsaved note');assert.equal(dirty.at(-1),true);
  await click(button(container,'Discard edits'));assert.equal(field(container,'Planning notes').value,'');assert.equal(dirty.at(-1),false);
});

test('camera setup publishes its exact setup selection and guards unsaved measurements',async t=>{
  const selections=[],dirty=[];const setup={setup_id:'setup-a',site_id:'a',source_id:'rtsp-a',camera_id:'cam-a',title:'Setup A',revision:1,status:'draft',pose:{},checkpoints:[],measurement_note:'',tolerance_m:2};
  const originalStatus=calibrationApi.status;calibrationApi.status=async()=>({status:'not_published',fusion:{acknowledged:false},can_rollback:false});t.after(()=>{calibrationApi.status=originalStatus;});
  mockApi(t,{request:async path=>{if(path==='/camera-setups/context')return{sites:[{...site('a'),cameras:[{camera_id:'cam-a',name:'Camera A'}]}],sources:[],sources_available:true};if(path==='/camera-setups')return{setups:[setup]};throw Error(path);}});
  const {container}=await mount(t,h(CameraSetupPanel,{onSelectSetup:(...args)=>selections.push(args),onDirtyChange:value=>dirty.push(value)}));
  assert.deepEqual(selections.at(-1),['setup-a','a']);
  await change(field(container,'Setup title'),'Changed setup');assert.equal(dirty.at(-1),true);
  await click(button(container,'Discard edits'));assert.equal(field(container,'Setup title').value,'Setup A');assert.equal(dirty.at(-1),false);
  await click(button(container,'New setup'));assert.deepEqual(selections.at(-1),[undefined,undefined]);
});

test('evidence interval and annotations remain guarded until package save or explicit discard',async t=>{
  const dirty=[],selection=[];const record={case_id:'case-a',title:'Case A',revision:1,video_id:'vid-a',analysis_id:'analysis-a',created_by:'reviewer',created_at:'2026-09-30',at_s:5,evidence:{source:'recorded_file',video:{duration_s:30,title:'Clip'},event:{at_s:5}}};
  mockApi(t,{request:async path=>{if(path==='/cases')return{cases:[record]};if(path==='/cases/case-a')return record;if(path==='/evidence-packages')return{packages:[]};if(path==='/review/videos/vid-a')return{media_url:'/api/review/videos/vid-a/media'};throw Error(path);}});
  const {container}=await mount(t,h(EvidencePackagePanel,{onSelect:(...args)=>selection.push(args),onDirtyChange:value=>dirty.push(value)}));
  assert.equal(selection.at(-1)[0],'case-a');assert.equal(dirty.at(-1),false);
  await change(field(container,'Clip start'),'3');assert.equal(dirty.at(-1),true);assert.equal(field(container,'Recorded case').disabled,true);
  await click(button(container,'Discard package draft'));assert.equal(field(container,'Clip start').value,'2');assert.equal(dirty.at(-1),false);
  await click(button(container,'Add annotation'));await change(field(container,'Observation'),'Observed movement');assert.equal(dirty.at(-1),true);
  await click(button(container,'Discard package draft'));assert.equal(container.querySelector('.package-annotation'),null);assert.equal(dirty.at(-1),false);
});

test('mission selection reports changed IDs and create drafts can be discarded',async t=>{
  const selections=[],dirty=[];const missions=['m-a','m-b'].map(id=>({mission_id:id,name:id,state:'draft',objectives:[],resources:[],comms:[],alert_ids:[],progress:{summary:'No objectives',percent:0,done:0,total:0,outstanding:[],unassigned:[]},legal_transitions:[],event_ids:[],replay:null}));
  mockApi(t,{missions:async()=>({missions}),mission:async id=>missions.find(row=>row.mission_id===id),zones:async()=>[],entities:async()=>[]});
  const {container}=await mount(t,h(MissionControlPanel,{onSelectMission:id=>selections.push(id),onDirtyChange:value=>dirty.push(value)}));
  await click(container.querySelectorAll('.mission-row')[1]);assert.equal(selections.at(-1),'m-b');
  await click([...container.querySelectorAll('button')].find(node=>node.textContent.includes('New mission')));
  await change(field(container,'Name'),'Draft response');assert.equal(dirty.at(-1),true);
  await click(container.querySelectorAll('.mission-row')[0]);assert.equal(selections.at(-1),'m-b');
  await click(button(container,'cancel'));assert.equal(dirty.at(-1),false);
  await click(container.querySelectorAll('.mission-row')[0]);assert.equal(selections.at(-1),'m-a');
});

test('calibration apply uses reviewed ticket and unsaved edits invalidate preview',async t=>{
  const original={...calibrationApi},applied=[];
  calibrationApi.status=async()=>({publication_id:null,status:'not_published',source_id:'rtsp-a',fusion:{acknowledged:false},can_rollback:false});
  calibrationApi.preview=async()=>({preview_id:'ticket-a',operation:'apply',setup_id:'setup-a',setup_revision:2,source_id:'rtsp-a',expires_at:new Date(Date.now()+60000).toISOString(),previous_pose:null,proposed_pose:{height_m:3},note:'Reviewed pose'});
  calibrationApi.apply=async preview=>{applied.push(preview);return{publication_id:'pub-a',status:'pending_fusion',source_id:'rtsp-a',fusion:{acknowledged:false},can_rollback:true,note:'Waiting for fusion'};};
  t.after(()=>Object.assign(calibrationApi,original));
  const {container,root}=await mount(t,h(CalibrationPublicationControls,{setupId:'setup-a',revision:2}));
  await click(button(container,'Preview calibration publication'));assert.ok(button(container,'Publish measured calibration'));
  await act(async()=>root.render(h(CalibrationPublicationControls,{setupId:'setup-a',revision:2,disabled:true})));
  assert.equal(button(container,'Publish measured calibration'),undefined);assert.equal(applied.length,0);
  await act(async()=>root.render(h(CalibrationPublicationControls,{setupId:'setup-a',revision:2})));
  await click(button(container,'Preview calibration publication'));await click(button(container,'Publish measured calibration'));
  assert.equal(applied[0].preview_id,'ticket-a');assert.match(container.textContent,/has not acknowledged/);
});

test('retained analyses can load an older page and open its exact analysis',async t=>{
  const {RetainedAnalyses}=await import('../src/components/RetainedAnalyses.tsx');
  const calls=[],selected=[];
  mockApi(t,{request:async path=>{calls.push(path);return path.includes('cursor=')?{analyses:[{analysis_id:'old-analysis',status:'completed',model:{name:'Old model'}}],next_cursor:null}:{analyses:[{analysis_id:'new-analysis',status:'completed',model:{name:'New model'}}],next_cursor:'opaque+/cursor'};}});
  const {container}=await mount(t,h(RetainedAnalyses,{videoId:'vid-a',disabled:false,onSelect:id=>selected.push(id)}));
  await click(button(container,'Load older analyses'));
  assert.match(calls[1],/cursor=opaque%2B%2Fcursor/);
  await change(container.querySelector('select'),'old-analysis');assert.equal(selected.at(-1),'old-analysis');assert.equal(button(container,'Load older analyses'),undefined);
});

test('browser history follows record selections and refuses a destination while dirty',async t=>{
  const {useState}=await import('react');const {useInvestigationHistory}=await import('../src/lib/use-investigation-history.ts');
  window.history.replaceState(null,'','/?view=missions&mission=m-a');
  let blocked=0;const moved=[];const oldGo=window.history.go;window.history.go=delta=>moved.push(delta);t.after(()=>{window.history.go=oldGo;});
  function Harness(){const [location,setLocation]=useState({tab:'missions',missionId:'m-a'}),[dirty,setDirty]=useState(false);useInvestigationHistory(location,null,next=>{setLocation(next);return true;},dirty,()=>blocked++);return h('div',{},h('span',{'data-current':true},location.missionId),h('button',{onClick:()=>setLocation({tab:'missions',missionId:'m-b'})},'Select B'),h('button',{onClick:()=>setDirty(true)},'Edit'),h('button',{onClick:()=>setDirty(false)},'Discard'));}
  const {container}=await mount(t,h(Harness));
  await click(button(container,'Select B'));assert.equal(new URLSearchParams(window.location.search).get('mission'),'m-b');
  await click(button(container,'Edit'));
  await act(async()=>{window.history.replaceState({sioNavigationIndex:0},'','/?view=missions&mission=m-a');window.dispatchEvent(new window.PopStateEvent('popstate',{state:{sioNavigationIndex:0}}));});
  assert.equal(blocked,1);assert.deepEqual(moved,[1]);assert.equal(container.querySelector('[data-current]').textContent,'m-b');
  await click(button(container,'Discard'));
  await act(async()=>window.dispatchEvent(new window.PopStateEvent('popstate',{state:{sioNavigationIndex:0}})));
  assert.equal(container.querySelector('[data-current]').textContent,'m-a');
});

test('operational requests do not supply a pretend authenticated actor',async t=>{
  const previous=fetchHandler,calls=[];
  fetchHandler=async(url,init)=>{calls.push({url:String(url),body:init?.body?JSON.parse(init.body):{},method:init?.method});return new Response(JSON.stringify({}));};
  t.after(()=>{fetchHandler=previous;});
  await api.acknowledgeAlert('a','Reviewed');await api.resolveAlert('a','Resolved');await api.approveDecision('d','option');await api.rejectDecision('d','Reason');await api.missionState('m','active');await api.assignResource('m','resource');await api.releaseResource('m','resource');await api.completeObjective('m','objective',true);await api.addComm('m','Observed');
  for(const call of calls){for(const key of ['ack_by','resolved_by','approved_by','rejected_by','by','author']){assert.equal(Object.hasOwn(call.body,key),false);assert.equal(new URL(call.url,'http://localhost').searchParams.has(key),false);}}
  assert.equal(calls.length,9);
});

test('site pose inputs stay protected until explicitly discarded or applied and saved',async t=>{
  const dirty=[],writes=[];
  const pose={lat:1,lon:2,bearing_deg:30,height_m:3,tilt_deg:20,fov_deg:70,vfov_deg:40,frame_width:640,frame_height:480};
  const first={...site('a'),cameras:['one','two'].map((id,index)=>({camera_id:id,name:`Camera ${id}`,source_id:`source-${id}`,x:index/2,y:0.5,pose:{...pose}}))};
  mockApi(t,{request:async(path,init)=>{if(path==='/sites')return{sites:[first,site('b')]};if(init?.method==='PUT'){const value=JSON.parse(init.body);writes.push(value);return{...first,...value,revision:2};}throw Error(path);}});
  const {container}=await mount(t,h(SitePanel,{onDirtyChange:value=>dirty.push(value)}));
  await click(container.querySelectorAll('.site-camera-choice')[0]);
  await change(field(container,'Latitude'),'5');assert.equal(dirty.at(-1),true);
  assert.equal(container.querySelector('select[aria-label="Site"]').disabled,true);assert.equal(container.querySelectorAll('.site-camera-choice')[1].disabled,true);assert.equal(button(container,'Save layout').disabled,true);
  const unload=new window.Event('beforeunload',{cancelable:true});window.dispatchEvent(unload);assert.equal(unload.defaultPrevented,true);
  await click(button(container,'Discard pose edits'));assert.equal(field(container,'Latitude').value,'1');assert.equal(dirty.at(-1),false);
  await change(field(container,'Latitude'),'5');await click(button(container,'Apply pose to draft'));assert.equal(dirty.at(-1),true);assert.equal(button(container,'Save layout').disabled,false);
  await click(button(container,'Save layout'));assert.equal(writes[0].cameras[0].pose.lat,5);assert.equal(dirty.at(-1),false);
});

test('site creation and unfinished labels report drafts and support explicit discard',async t=>{
  const dirty=[],created=[];
  mockApi(t,{request:async(path,init)=>{if(init?.method==='POST'){const {name}=JSON.parse(init.body);created.push(name);return{...site('new'),name};}if(path==='/sites')return{sites:[site('a')]};throw Error(path);}});
  const {container}=await mount(t,h(SitePanel,{onDirtyChange:value=>dirty.push(value)}));
  await change(field(container,'New site name'),'Pending yard');assert.equal(dirty.at(-1),true);
  await click(button(container,'Discard new site'));assert.equal(field(container,'New site name').value,'');assert.equal(dirty.at(-1),false);
  await change(field(container,'Zone / camera label'),'Unplaced zone');assert.equal(dirty.at(-1),true);assert.equal(container.querySelector('select[aria-label="Site"]').disabled,true);
  await click(button(container,'Cancel drawing'));assert.equal(field(container,'Zone / camera label').value,'');assert.equal(dirty.at(-1),false);
  await change(field(container,'New site name'),'New yard');await click(button(container,'Create site'));assert.deepEqual(created,['New yard']);assert.equal(dirty.at(-1),false);
});

test('queue query changes discard stale rows/cursors after failure and allow refreshed pagination',async t=>{
  const {ProcessingQueuePanel}=await import('../src/components/ProcessingQueuePanel.tsx');
  let failFinished=true;const calls=[];
  const job=(id,status)=>({job_id:id,video_id:id,video_title:id,analysis_id:'analysis-a',revision:1,status,progress:0,attempt:1,max_attempts:2,queued_at:'2026-09-30T00:00:00Z'});
  mockApi(t,{request:async path=>{const query=new URL(path,'http://local').searchParams;calls.push(query);if(query.get('view')==='active')return{jobs:[job('active-job','running')],next_cursor:'active-cursor'};if(failFinished)throw Error('Offline fixture');return query.get('cursor')?{jobs:[job('older-finished-job','completed')],next_cursor:null}:{jobs:[job('new-finished-job','completed')],next_cursor:'finished-cursor'};}});
  const {container}=await mount(t,h(ProcessingQueuePanel));assert.match(container.textContent,/active-job/);assert.equal(button(container,'Older jobs').disabled,false);
  await change(field(container,'Show jobs'),'finished');assert.equal(container.querySelectorAll('.ops-job').length,0);assert.equal(button(container,'Older jobs').disabled,true);assert.match(container.textContent,/Offline fixture/);
  failFinished=false;await click(button(container,'Refresh queue'));assert.equal(button(container,'Older jobs').disabled,false);
  await click(button(container,'Older jobs'));assert.equal(calls.at(-1).get('cursor'),'finished-cursor');assert.match(container.textContent,/older-finished-job/);assert.equal(button(container,'Older jobs').disabled,true);
});

for(const panel of ['sources','camera'])test(`${panel} guards navigation while source activation awaits its result`,async t=>{
  const {SourcesPanel}=await import('../src/components/SourcesPanel.tsx');
  const dirty=[];let resolveActivation;const pending=new Promise(resolve=>{resolveActivation=resolve;});
  const source={source_id:'rtsp-a',kind:'camera_rtsp',modality:'video',label:'Camera A',enabled:true,options:{},rate_hz:1,status:'ready',data_mode:'real',restart_required:false,last_success:null,error:null,last_observation:null};
  const setup={setup_id:'setup-a',site_id:'a',source_id:'rtsp-a',camera_id:'cam-a',title:'Setup A',revision:1,status:'draft',pose:{},checkpoints:[],measurement_note:'',tolerance_m:2};
  const result={activation_id:'applied-a',source_id:'rtsp-a',status:'verified',message:'Fixture response',rollback_available:true,verified_at:'2026-09-30T00:00:00Z',sample:null};
  const originalStatus=calibrationApi.status;calibrationApi.status=async()=>({status:'not_published',fusion:{acknowledged:false},can_rollback:false});t.after(()=>{calibrationApi.status=originalStatus;});
  mockApi(t,{request:async(path,init)=>{
    if(path==='/sources')return{sources:[source],registered_kinds:['camera_rtsp'],restart_required:false};
    if(path==='/camera-setups/context')return{sites:[{...site('a'),cameras:[{camera_id:'cam-a',name:'Camera A'}]}],sources:[source],sources_available:true};
    if(path==='/camera-setups')return{setups:[setup]};
    if(path==='/camera-setups/sources/rtsp-a/preview')return{available:false,reason:'Fixture'};
    if(path==='/sources/rtsp-a/activation/preview')return{preview_id:'ticket-a',source_id:'rtsp-a',action:'activate',expires_at:new Date(Date.now()+60000).toISOString(),before:null,after:{},changed_fields:['label'],warnings:[],requires_fresh_observation:true};
    if(path==='/sources/rtsp-a/activation')return init?.method==='POST'?pending:result;
    throw Error(path);
  }});
  const {container}=await mount(t,h(panel==='sources'?SourcesPanel:CameraSetupPanel,{onDirtyChange:value=>dirty.push(value)}));
  await click(button(container,'Preview activation'));await click(button(container,'Activate and verify incoming data'));
  assert.equal(dirty.at(-1),true);assert.equal(button(container,panel==='sources'?'Configure':'New setup').disabled,true);
  const before=new window.Event('beforeunload',{cancelable:true});window.dispatchEvent(before);assert.equal(before.defaultPrevented,true);
  await act(async()=>{resolveActivation(result);await pending;});assert.equal(dirty.at(-1),false);assert.equal(button(container,panel==='sources'?'Configure':'New setup').disabled,false);
  const after=new window.Event('beforeunload',{cancelable:true});window.dispatchEvent(after);assert.equal(after.defaultPrevented,false);
});
