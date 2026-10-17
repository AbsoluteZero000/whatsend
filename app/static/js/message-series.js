window.renderArabicOrdinal = function(number, words) {
  if(number<1||number>1000)return String(number);
  if(number<20)return words.units[number];
  if(number<100) {
    const tens=Math.floor(number/10),unit=number%10;
    return unit?(unit===1?words.compound_first:words.units[unit])+' و'+words.tens[tens]:words.tens[tens];
  }
  if(number===1000)return words.thousand;
  const hundreds=Math.floor(number/100),remainder=number%100;
  const base=hundreds===2&&remainder?words.two_hundred_after:words.hundreds[hundreds];
  return remainder?window.renderArabicOrdinal(remainder,words)+' '+words.after+' '+base:base;
};
window.createMessageSeries = function(config) {
  const mode = document.getElementById('message_mode');
  const panel = document.getElementById('series_builder');
  const root = document.getElementById('series_sources');
  const definition = document.getElementById('series_definition');
  const status = document.getElementById('series_status');
  const summary = document.getElementById('series_summary');
  const preview = document.getElementById('series_preview');
  const message = document.getElementById('message');
  const footer = document.getElementById('series_footer');
  const messageLabel = document.getElementById('message_label');
  const originalMessageLabel = messageLabel.textContent;
  const trigger = document.getElementById('trigger_type');
  const singleFile = document.getElementById('image');
  const submit = document.getElementById('submit_btn');
  let sources = [], lanes = [], pending = 0;
  const preparing = new Set(), importControls = new WeakMap();
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
      entries.push({message:current.map(entry=>entry.message).filter(text=>text.trim()).join('\n\n'),
        attachments:current.flatMap(entry=>entry.attachments),parts:current.flatMap(entry=>entry.parts||[])});
    }
    return entries;
  }
  function lineStreams() {
    return lanes.map(lane=>combine(sources.filter(source=>source.lane_id===lane.id).map(source=>
      source.entries.map((entry,index)=>{
        const values={content:entry.message,source_name:source.name,batch_number:String(index+1)};
        if(source.kind==='pdf') {
          values.book_name=source.name;
          const legacy=entry.start_page==null&&entry.end_page==null?entry.message.match(/^Pages (\d+)-(\d+)$/):null;
          const first=entry.start_page??(legacy?Number(legacy[1]):null),last=entry.end_page??(legacy?Number(legacy[2]):null);
          if(first!=null&&last!=null){values.start_page=String(first);values.end_page=String(last);}
        }
        const template=source.message_template??'{content}';
        const text=template.replace(/\{(content|source_name|batch_number|book_name|start_page|end_page)\}/g,(match,key)=>values[key]??match);
        return {...entry,message:text,parts:[{name:source.name,...entry,message:text}]};
      })),'sequential'));
  }
  function flattened() { return combine(lineStreams(),'parallel'); }
  function sync() {
    definition.value = JSON.stringify({strategy:'parallel',lanes,sources,footer:footer.value});
    const streams=lineStreams(), entries=combine(streams,'parallel');
    summary.textContent = (entries.length ? labels.summary.replace('{count}', entries.length) : labels.summaryEmpty)
      + (lanes.length > 1 ? ' · ' + lanes.length + ' ' + labels.tracks : '');
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
      const finalHeader=node('th',labels.finalMessage);finalHeader.scope='col';header.append(finalHeader);
      head.append(header);table.append(head);
      const body=node('tbody');
      entries.forEach((entry,i)=>{
        const row=node('tr',undefined,'series-preview-row'),th=node('th',labels.send+' #'+(i+1));th.scope='row';row.append(th);
        streams.forEach((stream,j)=>{
          const cell=node('td');cell.dataset.color=j%5;
          const item=stream[i];
          if(item) for(const part of item.parts) {
            const block=node('div',undefined,'series-preview-part');block.append(node('strong',part.name));
            if(part.message)block.append(node('p',part.message));
            if(part.attachments.length)block.append(node('small',part.attachments.map(asset=>asset.filename).join(' · ')));
            cell.append(block);
          } else cell.append(node('span','—','series-inactive'));
          row.append(cell);
        });
        const final=node('td');
        const text=[message.value,entry.message,footer.value].filter(part=>part.trim()).join('\n\n')
          .replace(/\{(send_number|send_ordinal_ar|counter|total_sends)\}/g,(_,key)=>key==='send_ordinal_ar'?window.renderArabicOrdinal(i+1,config.arabicOrdinals):String(key==='total_sends'?entries.length:i+1));
        final.append(node('p',text,'series-final-message'));
        if(entry.attachments.length)final.append(node('small',entry.attachments.map(asset=>asset.filename).join(' · ')));
        row.append(final);body.append(row);
      });
      table.append(body);
      const note=node('p',labels.previewHint,'field-note');
      const wrap=node('div',undefined,'series-plan-table-wrap');wrap.tabIndex=0;wrap.setAttribute('aria-label',labels.preview);wrap.append(table);
      preview.replaceChildren(note,wrap);
    }
    document.getElementById('message-form').dispatchEvent(new Event('composer:change'));
  }
  function newLane() { return {id:crypto.randomUUID(),name:labels.line+' '+(lanes.length+1),strategy:'sequential'}; }
  async function api(url, options) {
    options.headers={...(options.headers||{}),'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content};
    const response=await fetch(url,options);
    const data=await response.json().catch(()=>({}));
    if(!response.ok||response.redirected) throw new Error(typeof data.detail==='string'?data.detail:labels.importError);
    return data;
  }
  function spinner() {
    const indicator=node('span',undefined,'spinner series-spinner');indicator.setAttribute('aria-hidden','true');return indicator;
  }
  function importButton(label, source, action) {
    const control=button(label,()=>task(action,source),'btn btn-secondary');control.dataset.label=label;
    importControls.set(source,control);updateImportButton(source);return control;
  }
  function updateImportButton(source) {
    const control=importControls.get(source);if(!control)return;
    const busy=preparing.has(source);control.disabled=busy;control.setAttribute('aria-busy',String(busy));
    control.classList.toggle('series-importing',busy);
    const progress=source.kind==='pdf'?labels.splittingPdf:labels.fetchingPlaylist;
    control.replaceChildren(...(busy?[spinner(),node('span',progress)]:[document.createTextNode(control.dataset.label)]));
  }
  function importStatus(text, busy) {
    status.setAttribute('aria-busy',String(busy));status.classList.toggle('series-importing',busy);
    status.replaceChildren(...(busy?[spinner(),node('span',text)]:[document.createTextNode(text)]));
  }
  async function task(action, source) {
    if(source&&preparing.has(source))return;
    if(source){preparing.add(source);updateImportButton(source);}
    pending++; submit.disabled=true;importStatus(labels.importing,true);
    let result=labels.imported;
    try { await action(); }
    catch(error) { result=error.message||labels.importError; }
    finally {
      if(source){preparing.delete(source);updateImportButton(source);}
      pending--;submit.disabled=!!pending;importStatus(pending?labels.importing:result,!!pending);sync();
    }
  }
  function renderEntries(source, container) {
    container.replaceChildren();
    source.entries.forEach((entry,index)=>{
      const card=node('div',undefined,'series-entry');
      const header=node('div',undefined,'series-entry-header'); header.append(node('strong',labels.batch+' #'+(index+1)));
      card.append(header,node('p',entry.message,'series-final-message'));
      const files=node('div',undefined,'series-file-list');
      entry.attachments.forEach((asset,assetIndex)=>{
        const chip=node('span',undefined,'series-file'); const link=node('a',asset.filename);
        link.href='/jobs/series-files/'+encodeURIComponent(asset.id);link.target='_blank';link.rel='noopener';chip.append(link);
        files.append(chip);
      });card.append(files);
      container.append(card);
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
      // With a single track, reordering and removing it only get in the way.
      if(lanes.length>1) {
        const actions=node('div',undefined,'series-source-actions');
        for(const [delta,text,title] of [[-1,'↑',labels.moveUp],[1,'↓',labels.moveDown]]) {
          const move=button(text,()=>{[lanes[i],lanes[i+delta]]=[lanes[i+delta],lanes[i]];render();sync();});
          move.disabled=i+delta<0||i+delta>=lanes.length;move.setAttribute('aria-label',title);actions.append(move);
        }
        actions.append(button(labels.remove,()=>{
          sources=sources.filter(source=>source.lane_id!==lane.id);lanes.splice(i,1);render();sync();
        }));header.append(actions);
      }
      line.append(header);
      const hasContent=sources.some(source=>source.lane_id===lane.id);
      if(hasContent) line.append(node('p',labels.lineHint,'field-note series-line-hint'));
      const container=node('div',undefined,'series-line-sources');containers.set(lane.id,container);line.append(container);
      if(!hasContent) line.append(node('p',labels.emptyLine,'series-empty'));
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
      editor.append(node('summary',source.entries.length||source.kind==='messages'?labels.editSource:labels.prepareSource));
      const settings=node('div',undefined,'series-source-settings');editor.append(settings);card.append(editor);
      const templateLabel=node('label',labels.sourceTemplate,'series-field'),template=node('textarea');
      template.rows=3;template.maxLength=4096;template.value=source.message_template??'{content}';
      template.addEventListener('input',()=>{source.message_template=template.value;sync();});
      templateLabel.append(template);settings.append(templateLabel,node('p',labels.templateHint,'field-note'));
      settings.append(node('p',labels.sharedBatchTemplateHint,'field-note'));
      if(source.kind==='messages') {
        const bulkLabel=node('label',labels.messagesContent,'series-field'),bulk=node('textarea');
        bulk.rows=6;bulk.value=source.entries.map(entry=>entry.message).join('\n---\n');
        bulk.addEventListener('input',()=>{
          source.entries=bulk.value?bulk.value.split(/\r?\n---\r?\n/).map((text,i)=>({message:text,attachments:source.entries[i]?.attachments||[]})):[];
          renderEntries(source,entries);sync();
        });
        bulkLabel.append(bulk);settings.append(bulkLabel,node('p',labels.messagesContentHint,'field-note'));
      }
      if(source.kind==='pdf') {
        settings.append(node('p',labels.pdfTemplateHint,'field-note'));
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
        const load=importButton(labels.splitPdf,source,async()=>{
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
        });controls.append(load);settings.append(controls);settings.append(node('p',labels.pdfHint,'field-note'));
      } else if(source.kind==='playlist') {
        const controls=node('div',undefined,'series-import-grid');
        const url=field(controls,labels.playlistUrl,source.source_url||'','url');url.placeholder='https://youtu.be/…';
        const batch=field(controls,labels.itemsPerSend,source.items_per_send||2,'number');batch.min=1;batch.max=20;
        url.addEventListener('input',()=>{source.source_url=url.value;sync();});
        batch.addEventListener('input',()=>{source.items_per_send=Number(batch.value);sync();});
        controls.append(importButton(labels.importPlaylist,source,async()=>{
          const data=await api('/jobs/series-playlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url.value,items_per_send:Number(batch.value)})});
          source.entries=data.entries;source.name=data.title;source.source_url=data.source_url;source.items_per_send=Number(batch.value);source.skipped=data.skipped;render();
        }));settings.append(controls);settings.append(node('p',labels.playlistHint,'field-note'));
        if(source.skipped)settings.append(node('p',source.skipped+' '+labels.unavailableSkipped,'field-note'));
      }
      const content=node('details',undefined,'series-batches'); content.open=source.kind==='messages';
      const contentSummary=node('summary',source.entries.length+' '+labels.batches);contentSummary.dataset.sourceCount=index;content.append(contentSummary);
      const entries=node('div');renderEntries(source,entries);content.append(entries);
      settings.append(content);containers.get(source.lane_id).append(card);
    });
  }
  function add(kind, laneId) {
    if(sources.length>=20){status.textContent=labels.limit;return;}
    sources.push({kind,lane_id:laneId,name:kind==='messages'?labels.messages:kind==='pdf'?labels.pdfChunks:labels.playlist,
      message_template:kind==='pdf'?'{book_name}\nمن صفحة {start_page} إلى صفحة {end_page}':'{content}',
      entries:[]});
    render();sync();
  }
  function toggle() {
    const series=mode.value==='series';panel.hidden=!series;document.querySelector('.form-card').classList.toggle('plan-composer',series);
    // Start with one track so the first step is choosing what to send.
    if(series&&!lanes.length){lanes.push(newLane());render();}
    const help=document.getElementById('message_mode_help');
    if(help)help.textContent=help.dataset[series?'series':'repeat'];
    document.getElementById('repeat_attachment').hidden=series;singleFile.disabled=series;
    message.required=!series;
    messageLabel.textContent=series?labels.header:originalMessageLabel;
    for(const option of trigger.options) if(['now','date'].includes(option.value))option.disabled=series;
    if(series&&['now','date'].includes(trigger.value))trigger.value='cron';
    config.timing();sync();
  }
  function restore(raw) {
    try {
      const plan=typeof raw==='string'?JSON.parse(raw):raw;
      footer.value=plan.footer||'';
      sources=plan.sources||[];lanes=plan.lanes||[];
      if(lanes.length&&plan.strategy==='sequential') {
        const ordered=lanes.flatMap(lane=>{
          const members=sources.filter(source=>source.lane_id===lane.id);
          if(lane.strategy!=='parallel')return members;
          return [{kind:'messages',name:lane.name,entries:combine(members.map(source=>source.entries),'parallel')}];
        });
        const lane=newLane();lanes=[lane];sources=ordered;sources.forEach(source=>source.lane_id=lane.id);
      }
      // Older parallel sources become separate lines, preserving their send order.
      lanes=lanes.flatMap(lane=>{
        if(lane.strategy!=='parallel')return [{...lane,strategy:'sequential'}];
        return sources.filter(source=>source.lane_id===lane.id).map(source=>{
          const replacement={id:crypto.randomUUID(),name:(lane.name+' · '+source.name).slice(0,120),strategy:'sequential'};
          source.lane_id=replacement.id;return replacement;
        });
      });
      if(!lanes.length&&sources.length&&plan.strategy==='sequential') {
        const lane=newLane();lanes=[lane];sources.forEach(source=>source.lane_id=lane.id);
      } else if(!lanes.length&&sources.length) {
        lanes=sources.map((source,i)=>({id:crypto.randomUUID(),name:source.name||labels.line+' '+(i+1),strategy:'sequential'}));
        sources.forEach((source,i)=>source.lane_id=lanes[i].id);
      }
    } catch { sources=[];lanes=[];footer.value=''; }
    render();toggle();
  }
  panel.querySelector('[data-action="add-line"]').addEventListener('click',()=>{
    if(lanes.length>=20){status.textContent=labels.limit;return;}
    lanes.push(newLane());render();sync();
  });
  panel.querySelector('[data-action="preview"]').addEventListener('click',event=>{
    preview.hidden=!preview.hidden;event.currentTarget.setAttribute('aria-expanded',String(!preview.hidden));sync();
    if(!preview.hidden)preview.scrollIntoView({block:'nearest',behavior:'smooth'});
  });
  message.addEventListener('input',sync);footer.addEventListener('input',sync);mode.addEventListener('change',toggle);
  document.querySelectorAll('[data-message-variable]').forEach(element=>element.addEventListener('click',()=>{
    const variable='{'+element.dataset.messageVariable+'}';message.setRangeText(variable,message.selectionStart,message.selectionEnd,'end');message.focus();message.dispatchEvent(new Event('input',{bubbles:true}));
  }));
  restore(config.plan);
  return {restore,validate(){
    if(mode.value!=='series')return true;
    if(flattened().length>1000||sources.length>20){status.textContent=labels.limit;return false;}
    if(pending||lanes.some(lane=>!sources.some(source=>source.lane_id===lane.id))||sources.some(source=>!source.entries.length)||!sources.length){status.textContent=labels.completeImports;panel.scrollIntoView({block:'center'});return false;}
    if(flattened().some(entry=>!message.value.trim()&&!entry.message.trim()&&!footer.value.trim()&&!entry.attachments.length)){status.textContent=labels.emptyEntry;return false;}
    sync();return true;
  }};
};
