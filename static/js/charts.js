/* Minimal canvas line chart: crisp on high-DPI, theme aware, hover/touch readout. */
(function(){
  function css(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function niceTicks(lo, hi, n){
    const span = hi - lo || 1, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1,2,2.5,5,10].map(m=>m*mag).find(s=>s>=raw) || mag*10;
    const a = Math.floor(lo/step)*step, out=[];
    for(let v=a; v<=hi+step*.001; v+=step) out.push(+v.toPrecision(10));
    return out;
  }
  class LineChart{
    constructor(id, o){
      this.cv = document.getElementById(id); this.o = o; this.hover = null;
      if(!this.cv) return;
      this.ctx = this.cv.getContext('2d');
      new ResizeObserver(()=>{ this.size(); this.draw(); }).observe(this.cv);
      const mv = e => { const r=this.cv.getBoundingClientRect(); this.hover = e.clientX - r.left; this.draw(); };
      this.cv.addEventListener('pointermove', mv); this.cv.addEventListener('pointerdown', mv);
      ['pointerleave','pointercancel'].forEach(ev=>this.cv.addEventListener(ev, ()=>{ this.hover=null; this.draw(); }));
      this.size();
    }
    size(){
      const d = window.devicePixelRatio||1, w = this.cv.clientWidth, h = this.cv.clientHeight;
      if(!w||!h) return;
      if(this.cv.width !== Math.round(w*d) || this.cv.height !== Math.round(h*d)){ this.cv.width=Math.round(w*d); this.cv.height=Math.round(h*d); }
      this.ctx.setTransform(d,0,0,d,0,0); this.w=w; this.h=h;
    }
    draw(){
      if(!this.cv || !this.cv.clientWidth || this.cv.offsetParent===null) return;
      this.size(); const g=this.ctx, w=this.w, h=this.h, o=this.o, D=o.get();
      g.clearRect(0,0,w,h);
      const line=css('--line'), mute=css('--mute'), pal=o.colors();
      const ys=D.ys, N=Math.max(0,...ys.map(a=>a.length));
      let lo=o.min??Infinity, hi=o.max??-Infinity;
      if(o.min==null||o.max==null) ys.forEach(a=>a.forEach(v=>{ if(v!=null&&isFinite(v)){ if(o.min==null) lo=Math.min(lo,v); if(o.max==null) hi=Math.max(hi,v);} }));
      g.font='11px system-ui,sans-serif';
      if(!N||!isFinite(lo)||!isFinite(hi)){ g.fillStyle=mute; g.textAlign='center'; g.fillText(o.empty||'Waiting for data',w/2,h/2); g.textAlign='start'; return; }
      if(hi-lo<1e-6){ hi+=1; lo-=1; }
      const pad=(hi-lo)*.08; if(o.min==null) lo-=pad; if(o.max==null) hi+=pad;
      const ticks=niceTicks(lo,hi,4).filter(v=>v>=lo-1e-9&&v<=hi+1e-9);
      const L=46,R=10,T=10,B=22, X=i=>L+(w-L-R)*(N<2?0:i/(N-1)), Y=v=>T+(h-T-B)*(1-(v-lo)/(hi-lo));
      g.lineWidth=1; g.strokeStyle=line; g.fillStyle=mute;
      ticks.forEach(v=>{ const y=Math.round(Y(v))+.5; g.beginPath(); g.moveTo(L,y); g.lineTo(w-R,y); g.stroke();
        g.textAlign='right'; g.fillText(Number.isInteger(v)||Math.abs(v)>=10?v.toFixed(0):v.toFixed(1),L-6,y+4); });
      g.textAlign='start';
      (o.bands||[]).forEach(b=>{ if(b.y<lo||b.y>hi) return; g.setLineDash([4,5]); g.strokeStyle=css(b.color)||b.color; g.globalAlpha=.55; g.beginPath(); g.moveTo(L,Y(b.y)); g.lineTo(w-R,Y(b.y)); g.stroke(); g.setLineDash([]); g.globalAlpha=1; });
      const xl=D.xLabels||['','','']; g.fillStyle=mute; g.textAlign='left'; g.fillText(xl[0],L,h-6); g.textAlign='center'; g.fillText(xl[1],(L+w-R)/2,h-6); g.textAlign='right'; g.fillText(xl[2],w-R,h-6); g.textAlign='start';
      ys.forEach((a,si)=>{
        g.strokeStyle=pal[si%pal.length]; g.lineWidth=1.8; g.lineJoin='round'; g.beginPath(); let pen=false;
        const step=Math.max(1,Math.floor(a.length/(w*1.5)));
        for(let i=0;i<a.length;i+=step){ const v=a[i]; if(v==null||!isFinite(v)){ pen=false; continue; } pen?g.lineTo(X(i),Y(v)):(g.moveTo(X(i),Y(v)),pen=true); }
        g.stroke();
      });
      if(this.hover!=null && N>1){
        const i=Math.max(0,Math.min(N-1,Math.round((this.hover-L)/(w-L-R)*(N-1)))), x=X(i);
        g.strokeStyle=mute; g.setLineDash([3,3]); g.beginPath(); g.moveTo(x,T); g.lineTo(x,h-B); g.stroke(); g.setLineDash([]);
        const parts=ys.map((a,si)=>({n:o.names[si],v:a[i],c:pal[si%pal.length]})).filter(p=>p.v!=null);
        const head=D.xAt?D.xAt(i):''; const bw=Math.max(...parts.map(p=>g.measureText(p.n+' '+(+p.v).toFixed(2)).width),g.measureText(head).width)+26;
        let bx=x+10; if(bx+bw>w-4) bx=x-bw-10; const bh=16*(parts.length+(head?1:0))+8;
        g.fillStyle=css('--surface3'); g.strokeStyle=css('--line2'); g.beginPath(); g.roundRect(bx,T+2,bw,bh,8); g.fill(); g.stroke();
        let ty=T+18; g.fillStyle=mute; if(head){ g.fillText(head,bx+10,ty); ty+=16; }
        parts.forEach(p=>{ g.fillStyle=p.c; g.fillRect(bx+10,ty-8,8,8); g.fillStyle=css('--text'); g.fillText(p.n+' '+(+p.v).toFixed(2),bx+22,ty); ty+=16; });
      }
    }
  }
  window.LineChart = LineChart;
})();
