import test from 'node:test';
import assert from 'node:assert/strict';

test('browser uploads preserve a single media Content-Type and untouched file bytes through the shared SDK', async () => {
  const original={fetch:globalThis.fetch,window:globalThis.window,document:globalThis.document};
  const storage=()=>{const map=new Map();return {getItem:key=>map.get(key)??null,setItem:(key,value)=>map.set(key,value),removeItem:key=>map.delete(key)};};
  globalThis.window={sessionStorage:storage(),localStorage:storage()};
  globalThis.document={cookie:''};
  const token=`e30.${Buffer.from(JSON.stringify({sub:'upload-test',tenant:'test',roles:['operator'],exp:Math.floor(Date.now()/1000)+3600})).toString('base64url')}.signature`;
  const requests=[];
  const json=body=>new Response(JSON.stringify(body),{headers:{'Content-Type':'application/json'}});
  globalThis.fetch=async (url,init)=>{
    if(url==='/auth/config')return json({mode:'dev',required:true,oidc:null});
    if(url.startsWith('/auth/dev/token'))return json({access_token:token});
    requests.push({url,init,headers:new Headers(init.headers)});
    return json({video_id:'saved-video',record_id:'saved-floorplan'});
  };
  const session=await import('../src/lib/session.ts');
  try {
    // Import after installing the fetch stub: the real client captures its fetch implementation.
    const {reviewApi}=await import('../src/lib/review-api.ts');
    const {api}=await import('../src/lib/api.ts');
    await session.signIn();
    const clip=new File([new Uint8Array([0,0,0,20,102,116,121,112])],'door view.mp4',{type:'video/mp4'});
    const controller=new AbortController();
    await reviewApi.upload(clip,controller.signal);
    assert.equal(requests[0].url,'/api/review/videos');
    assert.equal(requests[0].headers.get('Content-Type'),'video/mp4');
    assert.equal(requests[0].headers.get('X-Filename'),'door%20view.mp4');
    assert.equal(requests[0].headers.get('Authorization'),`Bearer ${token}`);
    assert.equal(requests[0].init.body,clip);
    assert.equal(requests[0].init.signal,controller.signal);
    assert.deepEqual([...new Uint8Array(await requests[0].init.body.arrayBuffer())],[0,0,0,20,102,116,121,112]);
    for(const mime of ['image/png','image/jpeg']){
      const file=new File([new Uint8Array([1,2,3])],'floorplan',{type:mime});
      await api.request('/sites/site/floorplan',{method:'POST',body:file,headers:new Headers({'cOnTeNt-TyPe':mime})});
      const sent=requests.at(-1);
      assert.equal(sent.headers.get('content-type'),mime);
      assert.equal(sent.init.body,file);
    }
    await api.request('/cases/example',{method:'PATCH',body:JSON.stringify({expected_revision:1})});
    assert.equal(requests.at(-1).headers.get('content-type'),'application/json');
    assert.equal(requests.at(-1).init.body,'{"expected_revision":1}');
  } finally {session.clear();Object.assign(globalThis,original);}
});
