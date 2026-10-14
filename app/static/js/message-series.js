window.createMessageSeries = function(config) {
  const mode = document.getElementById('message_mode');
  const panel = document.getElementById('series_builder');
  const root = document.getElementById('series_sources');
  const definition = document.getElementById('series_definition');
  const strategy = document.getElementById('series_strategy');
  const status = document.getElementById('series_status');
  const summary = document.getElementById('series_summary');
  const preview = document.getElementById('series_preview');
  const message = document.getElementById('message');
  const trigger = document.getElementById('trigger_type');
  const singleFile = document.getElementById('image');
  const submit = document.getElementById('submit_btn');
  let sources = [], pending = 0;
  const labels = config.labels;
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function button(text, action, className='btn-clear') {
    const element = node('button', text, className); element.type = 'button'; element.addEventListener('click', action); return element;
  }
  function field(parent, label, value, type='text') {
    const wrap = node('label', undefined, 'series-field'); wrap.append(node('span', label));
    const input = node('input'); input.type = type; input.value = value; wrap.append(input); parent.append(wrap); return input;
  }
  function flattened() {
    if (strategy.value === 'sequential') return sources.flatMap(source=>source.entries);
    const entries=[];
    for (let i=0;i<Math.max(0,...sources.map(source=>source.entries.length));i++) {
      const current=sources.map(source=>source.entries[i]).filter(Boolean);
      entries.push({message:current.map(entry=>entry.message).filter(Boolean).join('\n\n'), attachments:current.flatMap(entry=>entry.attachments)});
    }
    return entries;
  }
  function sync() {
    definition.value = JSON.stringify({strategy:strategy.value,sources});
    const entries=flattened();
    summary.textContent = entries.length + ' ' + labels.scheduledSends + ' · ' + sources.length + ' ' + labels.sources;
    if (!preview.hidden) {
      const fragment=document.createDocumentFragment();
      entries.forEach((entry,i)=>{
        const row=node('div',undefined,'series-preview-row'); row.append(node('strong',labels.send+' #'+(i+1)));
        const text=[message.value,entry.message].filter(Boolean).join('\n\n');
        row.append(node('p',text));
        if(entry.attachments.length) row.append(node('small',entry.attachments.map(asset=>asset.filename).join(' · ')));
        fragment.append(row);
      });
      preview.replaceChildren(fragment);
    }
    document.getElementById('message-form').dispatchEvent(new Event('composer:change'));
  }
  async function api(url, options) {
    options.headers={...(options.headers||{}),'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content};
    const response=await fetch(url,options);
    const data=await response.json().catch(()=>({}));
    if(!response.ok||response.redirected) throw new Error(typeof data.detail==='string'?data.detail:labels.importError);
    return data;
  }
  async function task(action) {
    pending++; submit.disabled=true; status.textContent=labels.importing;
    try { await action(); status.textContent=labels.imported; }
    catch(error) { status.textContent=error.message||labels.importError; }
    finally { pending--; submit.disabled=!!pending; sync(); }
  }
  function renderEntries(source, container) {
    container.replaceChildren();
    source.entries.forEach((entry,index)=>{
      const card=node('div',undefined,'series-entry');
      const header=node('div',undefined,'series-entry-header'); header.append(node('strong',labels.batch+' #'+(index+1)));
      header.append(button(labels.remove,()=>{source.entries.splice(index,1);renderEntries(source,container);sync();})); card.append(header);
      const textarea=node('textarea'); textarea.value=entry.message; textarea.rows=3; textarea.placeholder=labels.entryMessage;
      textarea.setAttribute('aria-label',labels.batch+' '+(index+1)+' '+labels.message);
      textarea.addEventListener('input',()=>{entry.message=textarea.value;sync();});card.append(textarea);
      const files=node('div',undefined,'series-file-list');
      entry.attachments.forEach((asset,assetIndex)=>{
        const chip=node('span',undefined,'series-file'); const link=node('a',asset.filename);
        link.href='/jobs/series-files/'+encodeURIComponent(asset.id);link.target='_blank';link.rel='noopener';chip.append(link);
        chip.append(button('×',()=>{entry.attachments.splice(assetIndex,1);renderEntries(source,container);sync();}));files.append(chip);
      });card.append(files);
      const upload=node('input');upload.type='file';upload.multiple=true;upload.accept=singleFile.accept;
      upload.setAttribute('aria-label',labels.addAttachments);
      upload.addEventListener('change',()=>task(async()=>{
        for(const file of upload.files) {
          const body=new FormData();body.set('file',file);
          entry.attachments.push(await api('/jobs/series-files',{method:'POST',body}));
        }
        renderEntries(source,container);
      }));card.append(upload);container.append(card);
    });
  }
  function render() {
    root.replaceChildren();
    sources.forEach((source,index)=>{
      const card=node('section',undefined,'series-source');
      const header=node('div',undefined,'series-source-header');
      const name=node('input');name.value=source.name;name.maxLength=120;name.setAttribute('aria-label',labels.sourceName);
      name.addEventListener('input',()=>{source.name=name.value;sync();});header.append(name);
      const actions=node('div',undefined,'series-source-actions');
      if(index) actions.append(button('↑',()=>{[sources[index-1],sources[index]]=[sources[index],sources[index-1]];render();sync();}));
      if(index<sources.length-1) actions.append(button('↓',()=>{[sources[index+1],sources[index]]=[sources[index],sources[index+1]];render();sync();}));
      actions.append(button(labels.remove,()=>{sources.splice(index,1);render();sync();}));header.append(actions);card.append(header);
      if(source.kind==='pdf') {
        const controls=node('div',undefined,'series-import-grid');
        const pdf=field(controls,labels.pdfFile,'','file');pdf.accept='.pdf';
        const modeLabel=node('label',undefined,'series-field');modeLabel.append(node('span',labels.pdfMode));
        const rangeMode=node('select');
        for(const [value,text] of [['chunks',labels.automaticChunks],['custom',labels.customRanges]]) {
          const option=node('option',text);option.value=value;rangeMode.append(option);
        }
        rangeMode.value=source.range_mode||'chunks';modeLabel.append(rangeMode);controls.append(modeLabel);
        const automatic=node('div',undefined,'series-import-grid series-pdf-options');
        const start=field(automatic,labels.startPage,source.start_page||1,'number');start.min=1;
        const end=field(automatic,labels.endPage,source.end_page||'','number');end.min=1;end.placeholder=labels.lastPage;
        const pages=field(automatic,labels.pagesPerSend,source.pages_per_send||5,'number');pages.min=1;pages.max=500;
        for(const input of [start,end,pages])input.addEventListener('input',()=>{
          source.start_page=Number(start.value);source.end_page=end.value?Number(end.value):null;source.pages_per_send=Number(pages.value);sync();
        });
        const custom=node('div',undefined,'series-pdf-options');
        const rangeList=node('div',undefined,'series-range-list');custom.append(rangeList);
        source.ranges=source.ranges||[];
        function drawRanges() {
          rangeList.replaceChildren();
          source.ranges.forEach((selection,i)=>{
            const row=node('div',undefined,'series-range-row');row.append(node('strong',labels.batch+' #'+(i+1)));
            const first=field(row,labels.startPage,selection.start??'','number');first.min=1;
            const last=field(row,labels.endPage,selection.end??'','number');last.min=1;
            first.addEventListener('input',()=>{selection.start=first.value?Number(first.value):null;sync();});
            last.addEventListener('input',()=>{selection.end=last.value?Number(last.value):null;sync();});
            row.append(button(labels.remove,()=>{source.ranges.splice(i,1);drawRanges();sync();}));rangeList.append(row);
          });
        }
        custom.append(button(labels.addRange,()=>{
          if(source.ranges.length>=1000){status.textContent=labels.limit;return;}
          source.ranges.push({start:null,end:null});drawRanges();sync();
        },'btn btn-secondary'));
        custom.append(node('p',labels.rangeHint,'field-note'));
        function toggleRanges() {
          source.range_mode=rangeMode.value;
          automatic.hidden=rangeMode.value==='custom';custom.hidden=!automatic.hidden;
          automatic.querySelectorAll('input').forEach(input=>input.disabled=automatic.hidden);
          custom.querySelectorAll('input').forEach(input=>input.disabled=custom.hidden);
        }
        rangeMode.addEventListener('change',()=>{toggleRanges();sync();});
        drawRanges();toggleRanges();controls.append(automatic,custom);
        const load=button(labels.splitPdf,()=>task(async()=>{
          if(!pdf.files[0])throw new Error(labels.choosePdf);
          const body=new FormData();body.set('file',pdf.files[0]);body.set('range_mode',rangeMode.value);
          if(rangeMode.value==='custom')body.set('custom_ranges',JSON.stringify(source.ranges));
          else {
            body.set('start_page',start.value);body.set('pages_per_send',pages.value);
            if(end.value)body.set('end_page',end.value);
          }
          const data=await api('/jobs/series-pdf',{method:'POST',body});
          source.entries=data.entries;source.name=data.title;source.start_page=data.start_page;source.end_page=data.end_page;
          source.pages_per_send=data.pages_per_send;source.range_mode=data.range_mode;source.ranges=data.ranges;
          render();
        }),'btn btn-secondary');controls.append(load);card.append(controls);card.append(node('p',labels.pdfHint,'field-note'));
      } else if(source.kind==='playlist') {
        const controls=node('div',undefined,'series-import-grid');
        const url=field(controls,labels.playlistUrl,source.source_url||'','url');url.placeholder='https://www.youtube.com/playlist?list=…';
        const batch=field(controls,labels.itemsPerSend,source.items_per_send||2,'number');batch.min=1;batch.max=20;
        controls.append(button(labels.importPlaylist,()=>task(async()=>{
          const data=await api('/jobs/series-playlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url.value,items_per_send:Number(batch.value)})});
          source.entries=data.entries;source.name=data.title;source.source_url=data.source_url;source.items_per_send=Number(batch.value);source.skipped=data.skipped;render();
        }),'btn btn-secondary'));card.append(controls);card.append(node('p',labels.playlistHint,'field-note'));
        if(source.skipped)card.append(node('p',source.skipped+' '+labels.unavailableSkipped,'field-note'));
      }
      const content=node('details',undefined,'series-batches'); content.open=source.kind==='messages';
      content.append(node('summary',source.entries.length+' '+labels.batches));
      const entries=node('div');renderEntries(source,entries);content.append(entries);
      content.append(button(labels.addBatch,()=>{source.entries.push({message:'',attachments:[]});renderEntries(source,entries);sync();},'btn btn-secondary'));
      card.append(content);root.append(card);
    });
  }
  function add(kind) {
    if(kind!=='messages'&&sources.length===1&&sources[0].kind==='messages'&&sources[0].entries.length===1&&!sources[0].entries[0].message&&!sources[0].entries[0].attachments.length)sources=[];
    sources.push({kind,name:kind==='messages'?labels.messages:kind==='pdf'?labels.pdfChunks:labels.playlist,entries:kind==='messages'?[{message:'',attachments:[]}]:[]});
    render();sync();
  }
  function toggle() {
    const series=mode.value==='series';panel.hidden=!series;
    document.getElementById('repeat_attachment').hidden=series;singleFile.disabled=series;
    message.required=!series;
    for(const option of trigger.options) if(['now','date'].includes(option.value))option.disabled=series;
    if(series&&['now','date'].includes(trigger.value))trigger.value='cron';
    config.timing();sync();
  }
  function restore(raw) {
    try { const plan=typeof raw==='string'?JSON.parse(raw):raw;sources=plan.sources||[];strategy.value=plan.strategy||'sequential'; }
    catch { sources=[]; }
    if(!sources.length)sources=[{kind:'messages',name:labels.messages,entries:[{message:'',attachments:[]}]}];
    render();toggle();
  }
  panel.querySelectorAll('[data-series-source]').forEach(element=>element.addEventListener('click',()=>add(element.dataset.seriesSource)));
  panel.querySelector('[data-action="preview"]').addEventListener('click',()=>{preview.hidden=!preview.hidden;sync();});
  strategy.addEventListener('change',sync);message.addEventListener('input',sync);mode.addEventListener('change',toggle);
  document.querySelectorAll('[data-message-variable]').forEach(element=>element.addEventListener('click',()=>{
    const variable='{'+element.dataset.messageVariable+'}';message.setRangeText(variable,message.selectionStart,message.selectionEnd,'end');message.focus();message.dispatchEvent(new Event('input',{bubbles:true}));
  }));
  restore(config.plan);
  return {restore,validate(){
    if(mode.value!=='series')return true;
    if(flattened().length>1000||sources.length>20){status.textContent=labels.limit;return false;}
    if(pending||sources.some(source=>!source.entries.length)||!sources.length){status.textContent=labels.completeImports;panel.scrollIntoView({block:'center'});return false;}
    if(flattened().some(entry=>!message.value.trim()&&!entry.message.trim()&&!entry.attachments.length)){status.textContent=labels.emptyEntry;return false;}
    sync();return true;
  }};
};
