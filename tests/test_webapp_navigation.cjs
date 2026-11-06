/* Execute actual native HTML functions/listeners; no browser, network or provider. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const htmlPath=path.resolve(process.argv[2]||path.join(__dirname,'../webapp/index.html'));
const html=fs.readFileSync(htmlPath,'utf8');
function actualFunction(name,optional=false){
  const match=html.match(new RegExp('^  (?:async )?function '+name+'\\([^]*?^  }','m'));
  if(!match){if(optional)return '';throw Error('Actual function not found: '+name);}
  return match[0];
}
function actualListener(start,after=0){
  const pos=html.indexOf(start,after);assert.ok(pos>=0,'Actual listener start: '+start);
  const end=html.indexOf('\n  });',pos);assert.ok(end>pos,'Actual listener end');
  return html.slice(pos,end+'\n  });'.length);
}
function actualClose(selector){
  const marker='unboundDetailControls(',pos=html.indexOf(marker+(selector==='[data-my-supp-close]'?'"'+selector+'"':"'"+selector+"'"));
  assert.ok(pos>=0,'Actual Close registration: '+selector);
  const end=html.indexOf('\n    });',pos);assert.ok(end>pos);
  return html.slice(pos,end+'\n    });'.length);
}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
async function settle(){for(let n=0;n<6;n++)await Promise.resolve();}
function fixture(){
  const listeners=new Map(),controls=new Map(),calls={renders:0,focus:[],nativeClose:0,backShow:0,backHide:0,posts:[],gets:[]};
  const state={activeTab:'today',guide:{active:null},profileUi:{activeModule:'organs'},
    mySupplements:{open:true,active:[{supplement_id:1,name:'A',dose_text:'A own',note_text:''},{supplement_id:2,name:'B',dose_text:'B own',note_text:''},{supplement_id:3,name:'C',dose_text:'C own',note_text:''}],frozen:[]},
    complexHub:{open:false},suppHub:{open:false,loadVersion:0,loading:false,saving:false,slots:[],schedules:[],doseText:'',noteText:''}};
  const env={state,console,Promise,Date,Number,String,Boolean,Array,Set,Map,JSON,Math,
    coreOpen:false,panelOpen:false,scheduleRevision:0,nativeVisible:false,window:{Progressor:{}},activeRenderPointers:new Set(),normalizeHHMM:value=>/^\d\d:\d\d$/.test(String(value||''))?value:'',
    renderActiveTab:()=>{calls.renders++;},closeGuide:()=>{state.guide.active=null;},syncGuideOverlay:()=>{},saveNavigation:()=>{},isEditing:()=>false,
    loadMySupplements:async()=>{},loadToday:async()=>{},showToast:()=>{},CSS:{escape:value=>value},
    tg:{BackButton:{show:()=>{calls.backShow++;},hide:()=>{calls.backHide++;}}}};
  const document={activeElement:null,getElementById:()=>null,
    querySelector(selector){
      if(selector.includes('progressor-connections')&&env.coreOpen)return {id:'progressor-connections'};
      if(selector.includes('progressor-context')&&env.panelOpen)return {id:'progressor-context'};
      return null;
    },
    addEventListener(name,handler){if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push(handler);}};
  const node=(id,attrs=[])=>({id,attributes:attrs,disabled:false,type:'button',tagName:'BUTTON',isConnected:true,parentElement:null,
    getAttribute(name){return attrs.find(attr=>attr.name===name)?.value||null;},
    getBoundingClientRect:()=>({width:100,height:44}),setAttribute(){},
    focus(){calls.focus.push(id);if(!env.coreOpen||id.startsWith('core'))document.activeElement=this;},
    click(){calls.nativeClose++;controls.get('[data-my-supp-close]')?.click();}});
  const first=node('native-first',[{name:'data-my-supp-close',value:''}]),last=node('native-last'),opener=node('native-opener'),core=node('core-control');
  const native=node('native-dialog');native.classList=new Set(['card','sheet']);native.classList.contains=name=>native.classList.has(name);native.querySelectorAll=selector=>selector==='button'?[first,last]:[first,last];
  native.querySelector=()=>({textContent:'Native list'});native.contains=n=>[first,last,native].includes(n);
  const appEl={querySelectorAll:selector=>selector==='[role="dialog"]'&&env.nativeVisible?[native]:[]};
  native.parentElement=appEl;first.parentElement=native;last.parentElement=native;
  document.activeElement=opener;env.document=document;env.appEl=appEl;
  env.getComputedStyle=()=>({zIndex:'33'});
  env.unboundDetailControls=selector=>{const element={addEventListener(name,fn){this[name]=fn;}};controls.set(selector,element);return [element];};
  env.api=async(url,options={})=>{
    if(options.method==='POST'){calls.posts.push({url,body:JSON.parse(JSON.stringify(options.body))});if(env.writeGate&&url==='./api/my_supplements/update')await env.writeGate;return {ok:true};}
    calls.gets.push(url);if(url==='./api/schedule'){if(env.scheduleGate)await env.scheduleGate;return {slots:[{slot_id:'morning',title_ru:'Утро',time_local:'09:00'}],schedules:[{supplement_id:1,slot_id:'morning',time_hhmm:null}]};}
    throw Error('Unexpected synthetic read '+url);
  };
  vm.createContext(env);
  const names=['cancelSupplementHubOpening','openSupplementHub','persistSuppHub','activateTab','openMySupplements','openComplexHub','modalKey','nativeTopDialog','nativeBack','syncNativeNavigation'];
  for(const name of names)vm.runInContext(actualFunction(name,name==='cancelSupplementHubOpening'),env,{filename:htmlPath+':'+name});
  vm.runInContext("let nativeDialogKey='',nativeReturnFocus=null,nativeReturnSelector='';",env);
  vm.runInContext(actualListener("  document.addEventListener('progressor:overlay-open',()=>{"),env,{filename:htmlPath+':overlay-listener'});
  vm.runInContext(actualListener("  document.addEventListener('keydown',event=>{",html.indexOf('let nativeNavFrame')),env,{filename:htmlPath+':native-keyboard'});
  for(const selector of ['[data-my-supp-close]','[data-complex-hub-close]','[data-supp-hub-close]'])vm.runInContext(actualClose(selector),env,{filename:htmlPath+':'+selector});
  for(const selector of ['[data-complex-open-supp]','[data-my-supp-split]'])vm.runInContext(actualClose(selector),env,{filename:htmlPath+':'+selector});
  const retry=html.split('\n').find(line=>line.includes("unboundDetailControls('[data-supp-hub-retry]')"));assert.ok(retry);vm.runInContext(retry,env,{filename:htmlPath+':retry'});
  return {env,state,calls,first,last,core,native,opener,document,
    close:selector=>controls.get(selector).click(),control:selector=>controls.get(selector),
    fire(name,event={}){for(const handler of listeners.get(name)||[])handler(event);},
    key(key,shiftKey=false){const event={key,shiftKey,prevented:false,preventDefault(){this.prevented=true;}};this.fire('keydown',event);return event;}};
}
const results=[];
async function test(name,fn){try{await fn();results.push({name,passed:true});}catch(error){results.push({name,passed:false,error:String(error.stack||error)});}}
async function pending(){const t=fixture(),gate=deferred();t.state.suppHub.savePromise=gate.promise;const opening=t.env.openSupplementHub(2);await settle();assert.equal(t.state.suppHub.open,false);return {...t,gate,opening};}
async function release(t){t.gate.resolve(true);await t.opening;await settle();}
async function main(){
  let scripts=0;for(const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi))if(!/\bsrc=/.test(match[1])&&match[2].trim()){new vm.Script(match[2],{filename:htmlPath});scripts++;}
  await test('ordinary immediate own item opens without a Core dependency',async()=>{const t=fixture();await t.env.openSupplementHub(2);assert.equal(t.state.suppHub.open,true);assert.equal(t.state.suppHub.item.supplement_id,2);assert.equal(t.state.suppHub.doseText,'B own');assert.equal(t.calls.posts.length,0);});
  await test('queued item opens when the same parent task is still current',async()=>{const t=await pending();await release(t);assert.equal(t.state.suppHub.open,true);assert.equal(t.state.suppHub.item.supplement_id,2);assert.equal(t.calls.posts.length,0);});
  await test('parent My Supplements Close cancels queued item opening',async()=>{const t=await pending();t.close('[data-my-supp-close]');await release(t);assert.equal(t.state.suppHub.open,false);assert.equal(t.calls.gets.length,0);});
  await test('parent close then reopen cannot resurrect an old queued opening',async()=>{const t=await pending();t.close('[data-my-supp-close]');await t.env.openMySupplements();await release(t);assert.equal(t.state.suppHub.open,false);});
  await test('complex parent Close cancels queued item opening',async()=>{const t=fixture(),gate=deferred();t.state.mySupplements.open=false;t.state.complexHub.open=true;t.state.suppHub.savePromise=gate.promise;const opening=t.env.openSupplementHub(2);t.close('[data-complex-hub-close]');gate.resolve(true);await opening;assert.equal(t.state.suppHub.open,false);});
  await test('shared Core opening cancels queued native task even if parent remains',async()=>{const t=await pending();t.env.coreOpen=true;t.fire('progressor:overlay-open');await release(t);assert.equal(t.state.suppHub.open,false);assert.equal(t.calls.posts.length,0);});
  await test('opening then closing Core does not revive the old queued task',async()=>{const t=await pending();t.env.coreOpen=true;t.fire('progressor:overlay-open');t.env.coreOpen=false;await release(t);assert.equal(t.state.suppHub.open,false);});
  await test('shared subscription panel also cancels queued native task',async()=>{const t=await pending();t.env.panelOpen=true;t.fire('progressor:overlay-open');await release(t);assert.equal(t.state.suppHub.open,false);});
  await test('tab change and return do not revive old queued opening',async()=>{const t=await pending();t.env.activateTab('catalog');t.env.activateTab('today');await release(t);assert.equal(t.state.suppHub.open,false);});
  await test('a newer item request supersedes only the older opening',async()=>{const t=await pending();const latest=t.env.openSupplementHub(3);t.gate.resolve(true);await Promise.all([t.opening,latest]);assert.equal(t.state.suppHub.item.supplement_id,3);assert.equal(t.state.suppHub.open,true);assert.equal(t.calls.gets.length,1);});
  await test('native hub Close cancels its queued replacement',async()=>{const t=await pending();t.close('[data-supp-hub-close]');await release(t);assert.equal(t.state.suppHub.open,false);});
  await test('cancelled view never cancels or replays the existing own save',async()=>{
    const t=fixture(),gate=deferred(),hub=t.state.suppHub;hub.open=true;hub.item=t.state.mySupplements.active[0];hub.doseText='A own edited';hub.noteText='own note';hub.timeMode='slot';hub.slotId='morning';hub.schedules=[{supplement_id:1,slot_id:'morning',time_hhmm:null}];t.env.writeGate=gate.promise;
    const saving=t.env.persistSuppHub();await settle();assert.equal(t.calls.posts.length,1);t.close('[data-supp-hub-close]');const opening=t.env.openSupplementHub(2);t.close('[data-my-supp-close]');t.env.coreOpen=true;t.fire('progressor:overlay-open');gate.resolve(true);assert.equal(await saving,true);await opening;
    assert.equal(hub.open,false);assert.equal(hub.savePromise,null);assert.equal(hub.saving,false);
    assert.equal(t.calls.posts.length,2);assert.deepEqual(t.calls.posts.map(row=>row.url),['./api/my_supplements/update','./api/schedule/set']);assert.ok(t.calls.posts.every(row=>row.body.supplement_id===1));assert.equal(t.calls.posts[0].body.dose_text,'A own edited');
  });
  await test('native Tab and ShiftTab retain their own boundary behavior',async()=>{const t=fixture();t.env.nativeVisible=true;t.document.activeElement=t.last;assert.equal(t.key('Tab').prevented,true);assert.equal(t.document.activeElement,t.first);assert.equal(t.key('Tab',true).prevented,true);assert.equal(t.document.activeElement,t.last);});
  await test('native document Tab does not intercept open Core dialog',async()=>{const t=fixture();t.env.nativeVisible=true;t.env.coreOpen=true;t.document.activeElement=t.core;const event=t.key('Tab');assert.equal(event.prevented,false);assert.equal(t.document.activeElement,t.core);assert.deepEqual(t.calls.focus,[]);});
  await test('native document ShiftTab does not intercept open Core dialog',async()=>{const t=fixture();t.env.nativeVisible=true;t.env.coreOpen=true;t.document.activeElement=t.core;const event=t.key('Tab',true);assert.equal(event.prevented,false);assert.deepEqual(t.calls.focus,[]);});
  await test('native Escape closes its own sheet when Core is absent',async()=>{const t=fixture();t.env.nativeVisible=true;assert.equal(t.key('Escape').prevented,true);assert.equal(t.calls.nativeClose,1);assert.equal(t.state.mySupplements.open,false);});
  await test('native document Escape does not dismiss an underlying sheet beneath Core',async()=>{const t=fixture();t.env.nativeVisible=true;t.env.coreOpen=true;t.document.activeElement=t.core;assert.equal(t.key('Escape').prevented,false);assert.equal(t.calls.nativeClose,0);assert.equal(t.state.mySupplements.open,true);});
  await test('native focus synchronization yields completely to open Core',async()=>{const t=fixture();t.env.nativeVisible=true;t.env.coreOpen=true;t.document.activeElement=t.core;t.env.syncNativeNavigation();assert.deepEqual(t.calls.focus,[]);assert.equal(t.calls.backShow,0);assert.equal(t.calls.backHide,0);assert.equal(t.document.activeElement,t.core);});
  await test('native focus synchronization still works without Core',async()=>{const t=fixture();t.env.nativeVisible=true;t.env.syncNativeNavigation();assert.equal(t.document.activeElement,t.native);assert.equal(t.calls.backShow,1);});
  await test('actual complex item opener remains available',async()=>{const t=fixture();t.state.mySupplements.open=false;t.state.complexHub.open=true;t.control('[data-complex-open-supp]').dataset={complexOpenSupp:'2'};await t.control('[data-complex-open-supp]').click();assert.equal(t.state.suppHub.open,true);assert.equal(t.state.suppHub.item.supplement_id,2);assert.equal(t.calls.posts.length,0);});
  await test('actual active-card retry still loads the same own item',async()=>{const t=fixture();t.state.mySupplements.open=false;t.state.suppHub.open=true;t.state.suppHub.item=t.state.mySupplements.active[0];await t.control('[data-supp-hub-retry]').click();assert.equal(t.state.suppHub.open,true);assert.equal(t.state.suppHub.item.supplement_id,1);assert.equal(t.state.suppHub.loading,false);assert.equal(t.calls.gets.length,1);});
  await test('actual split continuation opens its explicitly created position',async()=>{
    const t=fixture(),hub=t.state.suppHub,original=t.env.api;t.state.mySupplements.open=false;hub.open=true;hub.item=t.state.mySupplements.active[0];
    t.env.api=async(url,options)=>{if(url==='./api/my_supplements/split'){t.calls.posts.push({url,body:JSON.parse(JSON.stringify(options.body))});return {created:{supplement_id:3}};}return original(url,options);};
    await t.control('[data-my-supp-split]').click();assert.equal(hub.open,true);assert.equal(hub.item.supplement_id,3);assert.equal(hub.splitDraft.originId,1);assert.equal(hub.splitDraft.newId,3);assert.equal(t.calls.posts.length,1);assert.equal(t.calls.posts[0].url,'./api/my_supplements/split');
  });
  await test('cancelling queued navigation leaves an already-open card read owned',async()=>{
    const t=fixture(),gate=deferred();t.env.scheduleGate=gate.promise;const loading=t.env.openSupplementHub(1);await settle();const version=t.state.suppHub.loadVersion;assert.equal(t.state.suppHub.open,true);assert.equal(t.state.suppHub.loading,true);
    t.env.coreOpen=true;t.fire('progressor:overlay-open');gate.resolve();await loading;assert.equal(t.state.suppHub.open,true);assert.equal(t.state.suppHub.loadVersion,version);assert.equal(t.state.suppHub.loading,false);assert.equal(t.state.suppHub.slots[0].slot_id,'morning');
  });
  await test('direct tab assignment rendered away and back retires queued navigation',async()=>{const t=await pending();vm.runInContext(actualFunction('renderActiveTab'),t.env);t.state.activeTab='catalog';t.env.renderActiveTab();t.state.activeTab='today';t.env.renderActiveTab();await release(t);assert.equal(t.state.suppHub.open,false);});
  await test('pending active-card retry state has no parent reference cycle',async()=>{const t=fixture(),gate=deferred();t.state.mySupplements.open=false;t.state.suppHub.open=true;t.state.suppHub.item=t.state.mySupplements.active[0];t.state.suppHub.savePromise=gate.promise;const opening=t.env.openSupplementHub(1);assert.doesNotThrow(()=>JSON.stringify(t.state));gate.resolve(true);await opening;assert.equal(t.state.suppHub.open,true);});
  const passed=results.every(row=>row.passed);
  console.log(JSON.stringify({passed,cases:results.length,passed_cases:results.filter(row=>row.passed).length,failed_cases:results.filter(row=>!row.passed).length,syntax_inline_scripts:scripts,html_sha256:crypto.createHash('sha256').update(Buffer.from(html)).digest('hex'),results,network_provider_or_browser_calls:0,source_extracted:true}));
  if(!passed)process.exitCode=1;
}
main().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
