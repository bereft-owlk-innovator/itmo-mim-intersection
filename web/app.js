/* Только отображение и управление. Позиции, сигналы и метрики считает Python. */
const $ = id => document.getElementById(id);
const canvas = $('map'), ctx = canvas.getContext('2d');
const sides = 'NESW', colors = {N:'#3783bb', E:'#258c80', S:'#ce923d', W:'#ac5676'};
let state = null, previous = null, received = 0, drag = null, view = {}, toastTimer;
const same = (a,b) => JSON.stringify(a) === JSON.stringify(b);
function toast(text) { $('toast').textContent=text; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,3000); }
async function post(path, data) {
  try {
    const response = await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || response.statusText);
    return true;
  } catch(error) { toast(error.message); return false; }
}
$('play').onclick=()=>post('control',{action:'toggle'});
$('step').onclick=()=>post('control',{action:'step'});
$('reset').onclick=()=>post('control',{action:'reset'});
$('speed').onchange=e=>post('control',{action:'speed',value:Number(e.target.value)});
$('edit').onchange=()=>{
  if ($('edit').checked) $('links').checked=true;
  $('hint').textContent=$('edit').checked ? 'Тяните заполненную вершину к пустой. Повторная связь или клик по линии — удалить.' : 'Включите редактирование, чтобы соединять полосы';
};
$('defaults').onclick=()=>{
  const connections=[];
  for(const side of sides) for(let lane=0;lane<3;lane++) connections.push([side,lane,sides[(sides.indexOf(side)+lane+1)%4],lane]);
  post('connections',{connections});
};
document.addEventListener('keydown',event=>{if(event.code==='Space' && event.target===document.body){event.preventDefault();$('play').click();}});

async function poll() {
  try {
    const response=await fetch('/api/state');
    if(!response.ok) throw Error('Сервер: '+response.status);
    const next=await response.json();
    if(state && next.revision!==state.revision) toast('Новый эксперимент · настройки Python применены');
    previous=state && next.revision===state.revision ? state : next;
    state=next; received=performance.now();
    document.body.classList.toggle('no-stats', !state.features.stats);
    document.body.classList.toggle('no-signals', !state.features.signals);
    document.body.classList.toggle('no-conflicts', !state.features.conflicts);
    $('status').textContent=state.error ? 'Ошибка Python' : state.running ? 'Моделирование' : 'На паузе';
    $('play').textContent=state.running ? 'Ⅱ Пауза' : '▶ Продолжить';
    $('speed').value=state.speed;
    const t=state.time; $('time').textContent=String(Math.floor(t/60)).padStart(2,'0')+':'+(t%60).toFixed(1).padStart(4,'0');
    $('revision').textContent='#'+(state.revision+1);
    for(const key of ['active','queue','conflicts','generated','completed','pending']) $(key).textContent=state.metrics[key];
    $('delay').textContent=state.metrics.completed ? state.metrics.delay.toFixed(1) : '—';
    $('error').hidden=!state.error; $('error').textContent=state.error;
    if(state.features.stats){
      chart('count-chart',[['active','#3783bb'],['queue','#ce923d']]);
      chart('delay-chart',[['delay','#258c80']]);
    }
  } catch(error) { $('status').textContent='Нет связи с Python'; $('error').hidden=false; $('error').textContent=error.message+' · Запустите python app.py'; }
  setTimeout(poll,70);
}

function rotate(x,y,side){for(let n=0;n<sides.indexOf(side);n++) [x,y]=[-y,x];return [500+x,500+y];}
function node(side,lane,incoming){return rotate((incoming?-1:1)*(16+lane*32),-96,side);}
function line(points,color,width=1,dash=[]){ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.strokeStyle=color;ctx.lineWidth=width;ctx.setLineDash(dash);ctx.stroke();ctx.setLineDash([]);}
function text(value,x,y,color='#778371',size=13,align='center'){ctx.fillStyle=color;ctx.font=`${size}px system-ui`;ctx.textAlign=align;ctx.fillText(value,x,y);}
function circle(x,y,r,fill,stroke){ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fillStyle=fill;ctx.fill();if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=2;ctx.stroke();}}
function road(side){
  ctx.save();ctx.translate(500,500);ctx.rotate(sides.indexOf(side)*Math.PI/2);
  ctx.fillStyle='#3b4451';ctx.fillRect(-96,-480,192,384);
  line([[-99,-480],[-99,-99]],'#d1d6c9',5);line([[99,-480],[99,-99]],'#d1d6c9',5);
  for(const x of [-64,-32,32,64]) line([[x,-480],[x,-105]],'#b0b8b3',1,[10,13]);
  for(const x of [-2,2]) line([[x,-480],[x,-100]],'#d5b676',1.5);
  line([[-94,-108],[-3,-108]],'#e5e5da',4);
  ctx.fillStyle='#62b3a318';ctx.fillRect(-95,-321,92,25);
  line([[-94,-310],[-3,-310]],'#66c2b0',2,[5,5]);
  for(let lane=0;lane<3;lane++){
    const x=-16-32*lane;
    circle(x,-310,4,'#81cbb9');
    // Стрелки на асфальте: вниз по входу, вверх по выходу.
    line([[x,-155],[x,-135]],'#cdd3ce55',2);
    line([[x-5,-141],[x,-135],[x+5,-141]],'#cdd3ce55',2);
    if(state?.features.signals){
      const lamps=state.signals[side][lane];
      ctx.fillStyle='#242e34';ctx.beginPath();ctx.roundRect(x-13,-204,26,18,4);ctx.fill();
      for(let k=0;k<3;k++) text(['←','↑','→'][k],x-8+k*8,-192,lamps[k]?'#83e0a3':'#d1786b',10);
    }
    text(String(lane),x,-220,'#9da7aa',10);
  }
  ctx.restore();
}
function draw(now){
  const rect=canvas.getBoundingClientRect(), dpr=Math.min(devicePixelRatio||1,2);
  if(canvas.width!==Math.round(rect.width*dpr)||canvas.height!==Math.round(rect.height*dpr)){canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);}
  ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,rect.width,rect.height);
  const scale=Math.min(rect.width,rect.height)/1050;
  view={scale,x:(rect.width-1000*scale)/2,y:(rect.height-1000*scale)/2};
  ctx.translate(view.x,view.y);ctx.scale(scale,scale);
  // Спокойная сетка и кварталы за пределами дороги.
  ctx.fillStyle='#d8dfcf';for(let x=25;x<1000;x+=30)for(let y=25;y<1000;y+=30){ctx.fillRect(x,y,1.5,1.5);}
  ctx.fillStyle='#e3e8db';for(const [x,y] of [[235,235],[645,235],[235,645],[645,645]]){ctx.beginPath();ctx.roundRect(x,y,120,120,18);ctx.fill();}
  ctx.fillStyle='#424e5e';ctx.fillRect(404,404,192,192);
  for(const side of sides) road(side);
  for(const [side,label] of [['N','СЕВЕР'],['E','ВОСТОК'],['S','ЮГ'],['W','ЗАПАД']]){
    const [x,y]=rotate(-165,-405,side);
    text(side,x,y,colors[side],24);text(label,x,y+20,'#83917d',10);
    if(state?.pending[side]) text('ожидают: '+state.pending[side],x,y+38,'#aa7353',12);
    const [cx,cy]=rotate(-166,-310,side);text('выбор полосы',cx,cy,'#579180',11);
  }
  if(state && $('links').checked){
    for(const route of state.routes){
      const [side,lane,target]=route.key,turn=(sides.indexOf(target)-sides.indexOf(side)+4)%4;
      const hot=drag && drag.side===side && drag.lane===lane;
      ctx.globalAlpha=drag ? hot?1:0.2 : $('edit').checked?0.9:0.48;
      line(route.points,{1:'#e3b268',2:'#cbdad9',3:'#72c6ab'}[turn],hot?4:2);
    }ctx.globalAlpha=1;
  }
  if(state){
    const before=new Map((previous?.cars||[]).map(c=>[c.id,c]));
    const u=Math.min(1,(now-received)/85);
    for(const car of state.cars){
      const old=before.get(car.id)||car;
      let da=car.angle-old.angle;while(da>Math.PI)da-=Math.PI*2;while(da<-Math.PI)da+=Math.PI*2;
      ctx.save();ctx.translate(old.x+(car.x-old.x)*u,old.y+(car.y-old.y)*u);ctx.rotate(old.angle+da*u);
      if(car.conflict){circle(0,0,16,'#ef846535');}
      ctx.shadowColor='#16212c55';ctx.shadowBlur=3;ctx.shadowOffsetY=2;
      ctx.fillStyle=colors[car.side];ctx.beginPath();ctx.roundRect(-10,-5.5,20,11,3);ctx.fill();ctx.shadowBlur=0;ctx.shadowOffsetY=0;
      ctx.fillStyle='#dce6e7bb';ctx.fillRect(3,-4,3,8);ctx.fillStyle='#24374688';ctx.fillRect(-7,-4,2,8);
      if(car.conflict){ctx.strokeStyle='#ff987d';ctx.lineWidth=2;ctx.strokeRect(-11,-6.5,22,13);}
      ctx.restore();
    }
  }
  if($('links').checked || $('edit').checked){
    for(const side of sides) for(let lane=0;lane<3;lane++) for(const incoming of [true,false]){
      const [x,y]=node(side,lane,incoming);
      circle(x,y,$('edit').checked?7:4,incoming?colors[side]:'#414c58',incoming?'#e9f0df':colors[side]);
    }
  }
  if(drag) line([node(drag.side,drag.lane,true),drag.point],'#f4d384',3,[6,5]);
  requestAnimationFrame(draw);
}

function world(event){const r=canvas.getBoundingClientRect();return [(event.clientX-r.left-view.x)/view.scale,(event.clientY-r.top-view.y)/view.scale];}
function hitNode(p,incoming){
  for(const side of sides)for(let lane=0;lane<3;lane++){const q=node(side,lane,incoming);if(Math.hypot(q[0]-p[0],q[1]-p[1])<11)return {side,lane};}
}
function hitRoute(p){
  let best=null,dist=8;
  for(const r of state?.routes||[])for(const q of r.points){const d=Math.hypot(q[0]-p[0],q[1]-p[1]);if(d<dist){dist=d;best=r;}}
  return best;
}
canvas.onpointerdown=event=>{
  if(!$('edit').checked || !state)return;
  const p=world(event),n=hitNode(p,true);
  if(n){drag={...n,point:p};canvas.setPointerCapture(event.pointerId);}
  else {const r=hitRoute(p);if(r)post('connections',{connections:state.connections.filter(c=>!same(c,r.key))});}
};
canvas.onpointermove=event=>{const p=world(event);if(drag)drag.point=p;canvas.style.cursor=$('edit').checked && (drag||hitNode(p,true)||hitRoute(p))?'crosshair':'default';};
canvas.onpointerup=event=>{
  if(!drag)return;
  const target=hitNode(world(event),false),source=drag;drag=null;
  if(!target||target.side===source.side){toast('Соедините вход с выходом на другой дороге');return;}
  const connection=[source.side,source.lane,target.side,target.lane];
  const exists=state.connections.some(c=>same(c,connection));
  post('connections',{connections:exists?state.connections.filter(c=>!same(c,connection)):[...state.connections,connection]});
};
canvas.onpointercancel=()=>drag=null;

function chart(id,series){
  const el=$(id),c=el.getContext('2d'),w=el.clientWidth,h=el.clientHeight,dpr=devicePixelRatio||1;
  el.width=w*dpr;el.height=h*dpr;c.scale(dpr,dpr);
  const rows=state.history.slice(-180),left=28,right=w-6,top=10,bottom=h-20;
  const max=Math.max(5,...rows.flatMap(r=>series.map(([key])=>r[key])))*1.1;
  c.font='9px system-ui';c.fillStyle='#8b9488';c.textAlign='right';
  for(let n=0;n<3;n++){const y=top+(bottom-top)*n/2;c.fillText(Math.round(max*(1-n/2)),left-6,y+3);c.strokeStyle='#e1e4db';c.beginPath();c.moveTo(left,y);c.lineTo(right,y);c.stroke();}
  const first=rows[0]?.t||0,last=rows.at(-1)?.t||1;
  for(const [key,color] of series){c.strokeStyle=color;c.lineWidth=1.8;c.beginPath();rows.forEach((r,i)=>{const x=left+(right-left)*(r.t-first)/Math.max(1,last-first),y=bottom-(bottom-top)*r[key]/max;i?c.lineTo(x,y):c.moveTo(x,y);});c.stroke();}
  c.fillStyle='#8b9488';c.textAlign='left';c.fillText(Math.floor(first)+' с',left,h-4);c.textAlign='right';c.fillText(Math.floor(last)+' с',right,h-4);
}
poll();requestAnimationFrame(draw);
