/* Canonical critical launch bridge; byte-identical native assets are distribution mirrors. */
(() => {
  'use strict';
  const source=document.currentScript?.dataset.progressorSource;
  const prefixes={gym:'/gymapi_prod/',food:'/api/',supply:'/supply/api/'};
  const nativeHeaders={gym:'X-Telegram-InitData',food:'X-Tg-Init-Data',supply:'X-Telegram-Init-Data'};
  if(!prefixes[source]||window.Progressor?._launchBridge)return;
  const key='progressor.launch.v1',rawFetch=window.fetch.bind(window);
  let saved={},renewing=null,retryAfter=0,transferable=false;
  try{saved=JSON.parse(sessionStorage.getItem(key)||'{}');}catch{}
  if(!saved||typeof saved!=='object'||Array.isArray(saved))saved={};
  const save=()=>{try{sessionStorage.setItem(key,JSON.stringify(saved));transferable=true;}catch{transferable=false;}};
  function capture(){
    let value='';
    for(const raw of [location.search,location.hash.replace(/^#/, '')]){
      const params=new URLSearchParams(raw);value=params.get('tgWebAppData')||params.get('initData')||'';
      if(value)break;
    }
    // Telegram's SDK may retain the original bot's proof across navigation.
    // Only an explicit URL launch identifies a new receiving bot; a different
    // SDK delivery must not relabel the saved launch as this other product.
    if(!value&&(!saved.launchData||saved.source===source))value=window.Telegram?.WebApp?.initData||'';
    if(value&&value!==saved.launchData){saved={source,launchData:value};retryAfter=0;save();}
  }
  capture();
  save();
  const valid=()=>!!(saved.session?.assertions&&saved.session.expires_at>Date.now()/1000+5);
  async function bootstrap(force=false){
    capture();
    if(!force&&valid())return saved.session;
    if(renewing)return renewing;
    if(!force&&Date.now()<retryAfter)return null;
    renewing=(async()=>{
      if(!saved.launchData||!Object.hasOwn(prefixes,saved.source))return null;
      const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),1500);
      try{
        const response=await rawFetch('/progressor/api/v1/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:saved.source,init_data:saved.launchData}),signal:controller.signal,cache:'no-store'});
        if(!response.ok)throw Error('unavailable');
        const value=await response.json();
        if(!value.assertions||!value.expires_at)throw Error('invalid_session');
        saved.session=value;retryAfter=0;save();return value;
      }catch{retryAfter=Date.now()+15000;return null;}
      finally{clearTimeout(timer);}
    })();
    try{return await renewing;}finally{renewing=null;}
  }
  const bridge={source,getState:()=>saved,sessionValid:valid,canNavigate:()=>transferable,bootstrap};
  window.Progressor={launchData:saved.launchData||'',renew:bootstrap,_launchBridge:bridge};
  window.fetch=async(input,options={})=>{
    const url=new URL(input instanceof Request?input.url:String(input),location.href);
    const audience=url.origin!==location.origin?null:url.pathname.startsWith(prefixes[source])?source:url.pathname.startsWith('/progressor/api/v1/me/')?'core':null;
    if(!audience)return rawFetch(input,options);
    const headers=new Headers(input instanceof Request?input.headers:undefined);
    new Headers(options.headers||{}).forEach((v,k)=>headers.set(k,v));
    // A native launch has its own valid server-checked proof. Core availability
    // must not delay it. Cross-product navigation still needs a Core assertion.
    if(!valid()&&audience===source&&saved.source===source&&headers.has(nativeHeaders[source])){
      void bootstrap();return rawFetch(input,options);
    }
    const session=await bootstrap();
    if(!session||!valid()||typeof session.assertions[audience]!=='string')return rawFetch(input,options);
    headers.set('X-Progressor-Auth',session.assertions[audience]);
    return rawFetch(input,{...options,headers});
  };
  window.Progressor.ready=bootstrap();
  // Reserve the normal bar before first paint; loading Core's UI is asynchronous.
  document.documentElement.dataset.progressorEarly=source;
  const style=document.createElement('style');style.id='progressor-early-layout';
  style.textContent=':root{--progressor-bar-height:48px}html[data-progressor-early="gym"] body,html[data-progressor-early="food"] body{padding-top:var(--progressor-bar-height)}html[data-progressor-early="gym"] .app-header{top:var(--progressor-bar-height)}html[data-progressor-early="supply"] #app{height:calc(100% - var(--progressor-bar-height));margin-top:var(--progressor-bar-height)}';
  document.head.append(style);
  const ui=document.createElement('script');ui.src='/progressor/assets/navigation.v1.js';ui.dataset.progressorSource=source;ui.async=true;
  const removeEmptySpace=()=>{if(!document.getElementById('progressor-shell'))style.remove();};
  ui.addEventListener('error',removeEmptySpace,{once:true});document.head.append(ui);
  setTimeout(removeEmptySpace,2000);
})();
