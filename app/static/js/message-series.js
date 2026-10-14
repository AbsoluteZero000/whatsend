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
  let sources = [], lanes = [], pending = 0;
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
    const input = node('input'); input.type = type; input.value = value;
    // Import settings prepare batches separately; only prepared content is submitted with the schedule.
    input.setAttribute('form','series-source-import');wrap.append(input); parent.append(wrap); return input;
  }
  function combine(streams, flow) {
    if (flow === 'sequential') return streams.flat();
    const entries=[];
    for(let i=0;i<Math.max(0,...streams.map(stream=>stream.length));i++) {
      const current=streams.map(stream=>stream[i]).filter(Boolean);
      entries.push({message:current.map(entry=>entry.message).filter(Boolean).join('\n\n'),
        attachments:current.flatMap(entry=>entry.attachments),parts:current.flatMap(entry=>entry.parts||[])});
    }
    return entries;
  }
  function lineStreams() {
    return lanes.map(lane=>combine(sources.filter(source=>source.lane_id===lane.id).map(source=>
      source.entries.map(entry=>({...entry,parts:[{name:source.name,...entry}]}))),lane.strategy));
  }
  function flattened() { return combine(lineStreams(),strategy.value); }
  function sync() {
    definition.value = JSON.stringify({strategy:strategy.value,lanes,sources});
    const streams=lineStreams(), entries=combine(streams,strategy.value);
    summary.textContent = entries.length + ' ' + labels.scheduledSends + ' · ' + lanes.length + ' ' + labels.lines + ' · ' + sources.length + ' ' + labels.sources;
    panel.querySelectorAll('[data-plan-strategy]').forEach(element=>element.setAttribute('aria-pressed',String(element.dataset.planStrategy===strategy.value)));
    panel.querySelectorAll('[data-line-count]').forEach(element=>{
      const i=lanes.findIndex(lane=>lane.id===element.dataset.lineCount);
      element.textContent=(streams[i]?.length||0)+' '+labels.batches;
    });
    panel.querySelectorAll('[data-source-count]').forEach(element=>{
      element.textContent=sources[Number(element.dataset.sourceCount)].entries.length+' '+labels.batches;
    });
    if (!preview.hidden) {
      const table=node('table',undefined,'series-plan-table'),head=node('thead'),header=node('tr');
      const number=node('th',labels.send);number.scope='col';header.append(number);
      lanes.forEach((lane,i)=>{const th=node('th',lane.name||labels.line);th.scope='col';th.dataset.color=i%5;header.append(th);});
      head.append(header);table.append(head);
      const body=node('tbody');
      let offset=0;const offsets=streams.map(stream=>{const start=offset;offset+=stream.length;return start;});
      entries.forEach((entry,i)=>{
        const row=node('tr',undefined,'series-preview-row'),th=node('th',labels.send+' #'+(i+1));th.scope='row';row.append(th);
        streams.forEach((stream,j)=>{
          const cell=node('td');cell.dataset.color=j%5;
          const item=stream[strategy.value==='parallel'?i:i-offsets[j]];
          if(item) for(const part of item.parts) {
            const block=node('div',undefined,'series-preview-part');block.append(node('strong',part.name));
            if(part.message)block.append(node('p',part.message));
            if(part.attachments.length)block.append(node('small',part.attachments.map(asset=>asset.filename).join(' · ')));
            cell.append(block);
          } else cell.append(node('span','—','series-inactive'));
          row.append(cell);
        });body.append(row);
      });
      table.append(body);
      const note=node('p',labels.commonPreview,'field-note');
      if(message.value) {note.append(node('span',message.value,'series-common-preview'));}
      const wrap=node('div',undefined,'series-plan-table-wrap');wrap.tabIndex=0;wrap.setAttribute('aria-label',labels.preview);wrap.append(table);
      preview.replaceChildren(note,wrap);
    }
    document.getElementById('message-form').dispatchEvent(new Event('composer:change'));
  }
  function choose(parent,label,value,options) {
    const wrap=node('label',undefined,'series-field');wrap.append(node('span',label));
    const select=node('select');
    select.setAttribute('aria-label',label);
    options.forEach(([id,text])=>{const option=node('option',text);option.value=id;select.append(option);});
    select.value=value;wrap.append(select);parent.append(wrap);return select;
  }
  function flowOptions() { return [['sequential',labels.inOrder],['parallel',labels.together]]; }
  function newLane() { return {id:crypto.randomUUID(),name:labels.line+' '+(lanes.length+1),strategy:'sequential'}; }
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
    const containers=new Map();
    lanes.forEach((lane,i)=>{
      const line=node('section',undefined,'series-line');line.dataset.color=i%5;
      const header=node('div',undefined,'series-line-header');
      header.append(node('span',String(i+1).padStart(2,'0'),'series-line-number'));
      const name=node('input');name.value=lane.name;name.maxLength=120;name.setAttribute('aria-label',labels.lineName);
      name.addEventListener('input',()=>{lane.name=name.value;sync();});header.append(name);
      const count=node('span',undefined,'series-count');count.dataset.lineCount=lane.id;header.append(count);
      const actions=node('div',undefined,'series-source-actions');
      for(const [delta,text,title] of [[-1,'↑',labels.moveUp],[1,'↓',labels.moveDown]]) {
        const move=button(text,()=>{[lanes[i],lanes[i+delta]]=[lanes[i+delta],lanes[i]];render();sync();});
        move.disabled=i+delta<0||i+delta>=lanes.length;move.setAttribute('aria-label',title);actions.append(move);
      }
      actions.append(button(labels.remove,()=>{
        sources=sources.filter(source=>source.lane_id!==lane.id);lanes.splice(i,1);render();sync();
      }));header.append(actions);line.append(header);
      const settings=node('div',undefined,'series-line-settings');
      const flow=choose(settings,labels.insideLine,lane.strategy,flowOptions());
      flow.addEventListener('change',()=>{lane.strategy=flow.value;sync();});
      settings.append(node('p',labels.lineHint,'field-note'));line.append(settings);
      const container=node('div',undefined,'series-line-sources');containers.set(lane.id,container);line.append(container);
      const addButtons=node('div',undefined,'series-source-buttons');
      for(const [kind,text] of [['messages',labels.messages],['pdf',labels.book],['playlist',labels.playlist]]) {
        const addButton=button('+ '+text,()=>add(kind,lane.id),'btn btn-secondary');addButton.dataset.seriesSource=kind;addButtons.append(addButton);
      }
      line.append(addButtons);root.append(line);
    });
    sources.forEach((source,index)=>{
      const siblings=sources.filter(item=>item.lane_id===source.lane_id),position=siblings.indexOf(source);
      const card=node('section',undefined,'series-source');
      const header=node('div',undefined,'series-source-header');
      const icon=node('span',source.kind==='pdf'?'PDF':source.kind==='playlist'?'↗':'Aa','series-source-icon');header.append(icon);
      const name=node('input');name.value=source.name;name.maxLength=120;name.setAttribute('aria-label',labels.sourceName);
      name.addEventListener('input',()=>{source.name=name.value;sync();});header.append(name);
      const sourceCount=node('span',source.entries.length+' '+labels.batches,'series-count');sourceCount.dataset.sourceCount=index;header.append(sourceCount);
      const actions=node('div',undefined,'series-source-actions');
      for(const [delta,text,title] of [[-1,'↑',labels.moveUp],[1,'↓',labels.moveDown]]) {
        const move=button(text,()=>{
          const other=sources.indexOf(siblings[position+delta]);[sources[index],sources[other]]=[sources[other],sources[index]];render();sync();
        });move.disabled=position+delta<0||position+delta>=siblings.length;move.setAttribute('aria-label',title);actions.append(move);
      }
      actions.append(button(labels.remove,()=>{sources.splice(index,1);render();sync();}));header.append(actions);card.append(header);
      const editor=node('details',undefined,'series-source-editor');editor.open=!source.entries.length||source.kind==='messages';
      editor.append(node('summary',source.entries.length?labels.editSource:labels.prepareSource));
      const settings=node('div',undefined,'series-source-settings');editor.append(settings);card.append(editor);
      if(lanes.length>1) {
        const destination=choose(settings,labels.moveToLine,source.lane_id,lanes.map(lane=>[lane.id,lane.name]));
        destination.addEventListener('change',()=>{source.lane_id=destination.value;render();sync();});
      }
      if(source.kind==='pdf') {
        const controls=node('div',undefined,'series-import-grid');
        const pdf=field(controls,labels.pdfFile,'','file');pdf.accept='.pdf';
        const modeLabel=node('label',undefined,'series-field');modeLabel.append(node('span',labels.pdfMode));
        const rangeMode=node('select');
        rangeMode.setAttribute('aria-label',labels.pdfMode);
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
        }),'btn btn-secondary');controls.append(load);settings.append(controls);settings.append(node('p',labels.pdfHint,'field-note'));
      } else if(source.kind==='playlist') {
        const controls=node('div',undefined,'series-import-grid');
        const url=field(controls,labels.playlistUrl,source.source_url||'','url');url.placeholder='https://www.youtube.com/playlist?list=…';
        const batch=field(controls,labels.itemsPerSend,source.items_per_send||2,'number');batch.min=1;batch.max=20;
        url.addEventListener('input',()=>{source.source_url=url.value;sync();});
        batch.addEventListener('input',()=>{source.items_per_send=Number(batch.value);sync();});
        controls.append(button(labels.importPlaylist,()=>task(async()=>{
          const data=await api('/jobs/series-playlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url.value,items_per_send:Number(batch.value)})});
          source.entries=data.entries;source.name=data.title;source.source_url=data.source_url;source.items_per_send=Number(batch.value);source.skipped=data.skipped;render();
        }),'btn btn-secondary'));settings.append(controls);settings.append(node('p',labels.playlistHint,'field-note'));
        if(source.skipped)settings.append(node('p',source.skipped+' '+labels.unavailableSkipped,'field-note'));
      }
      const content=node('details',undefined,'series-batches'); content.open=source.kind==='messages';
      content.append(node('summary',source.entries.length+' '+labels.batches));
      const entries=node('div');renderEntries(source,entries);content.append(entries);
      content.append(button(labels.addBatch,()=>{source.entries.push({message:'',attachments:[]});renderEntries(source,entries);sync();},'btn btn-secondary'));
      settings.append(content);containers.get(source.lane_id).append(card);
    });
  }
  function add(kind, laneId) {
    if(sources.length>=20){status.textContent=labels.limit;return;}
    const members=sources.filter(source=>source.lane_id===laneId);
    if(kind!=='messages'&&members.length===1&&members[0].kind==='messages'&&members[0].entries.length===1&&!members[0].entries[0].message&&!members[0].entries[0].attachments.length)
      sources=sources.filter(source=>source!==members[0]);
    sources.push({kind,lane_id:laneId,name:kind==='messages'?labels.messages:kind==='pdf'?labels.pdfChunks:labels.playlist,
      entries:kind==='messages'?[{message:'',attachments:[]}]:[]});
    render();sync();
  }
  function toggle() {
    const series=mode.value==='series';panel.hidden=!series;document.querySelector('.form-card').classList.toggle('plan-composer',series);
    document.getElementById('repeat_attachment').hidden=series;singleFile.disabled=series;
    message.required=!series;
    for(const option of trigger.options) if(['now','date'].includes(option.value))option.disabled=series;
    if(series&&['now','date'].includes(trigger.value))trigger.value='cron';
    config.timing();sync();
  }
  function restore(raw) {
    try {
      const plan=typeof raw==='string'?JSON.parse(raw):raw;
      sources=plan.sources||[];lanes=plan.lanes||[];strategy.value=plan.strategy||'parallel';
      if(!lanes.length&&sources.length) {
        lanes=sources.map((source,i)=>({id:crypto.randomUUID(),name:source.name||labels.line+' '+(i+1),strategy:'sequential'}));
        sources.forEach((source,i)=>source.lane_id=lanes[i].id);
      }
    } catch { sources=[];lanes=[];strategy.value='parallel'; }
    if(!lanes.length) {
      const lane=newLane();lanes=[lane];sources=[{kind:'messages',lane_id:lane.id,name:labels.messages,entries:[{message:'',attachments:[]}]}];
    }
    render();toggle();
  }
  panel.querySelector('[data-action="add-line"]').addEventListener('click',()=>{
    if(lanes.length>=20){status.textContent=labels.limit;return;}
    lanes.push(newLane());render();sync();
  });
  panel.querySelectorAll('[data-plan-strategy]').forEach(element=>element.addEventListener('click',()=>{
    strategy.value=element.dataset.planStrategy;sync();
  }));
  panel.querySelector('[data-action="preview"]').addEventListener('click',event=>{
    preview.hidden=!preview.hidden;event.currentTarget.setAttribute('aria-expanded',String(!preview.hidden));sync();
    if(!preview.hidden)preview.scrollIntoView({block:'nearest',behavior:'smooth'});
  });
  strategy.addEventListener('change',sync);message.addEventListener('input',sync);mode.addEventListener('change',toggle);
  document.querySelectorAll('[data-message-variable]').forEach(element=>element.addEventListener('click',()=>{
    const variable='{'+element.dataset.messageVariable+'}';message.setRangeText(variable,message.selectionStart,message.selectionEnd,'end');message.focus();message.dispatchEvent(new Event('input',{bubbles:true}));
  }));
  restore(config.plan);
  return {restore,validate(){
    if(mode.value!=='series')return true;
    if(flattened().length>1000||sources.length>20){status.textContent=labels.limit;return false;}
    if(pending||lanes.some(lane=>!sources.some(source=>source.lane_id===lane.id))||sources.some(source=>!source.entries.length)||!sources.length){status.textContent=labels.completeImports;panel.scrollIntoView({block:'center'});return false;}
    if(flattened().some(entry=>!message.value.trim()&&!entry.message.trim()&&!entry.attachments.length)){status.textContent=labels.emptyEntry;return false;}
    sync();return true;
  }};
};
