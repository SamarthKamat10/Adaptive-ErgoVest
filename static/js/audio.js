/* Browser-speaker alert. Rules: OFF by default, enabling makes NO sound, plays only while
   update(true) is called, stops immediately on update(false) / disable(), never overlaps loops. */
(function(){
  const AC = window.AudioContext || window.webkitAudioContext;
  let ctx=null, enabled=false, playing=false, timer=null, vol=.6, live=[];
  const stats={beeps:0, starts:0, stops:0};
  function pattern(){
    if(!playing||!ctx) return;
    if(ctx.state==='suspended') ctx.resume();
    const t0=ctx.currentTime;
    [0,.24,.48].forEach(off=>{
      const o=ctx.createOscillator(), g=ctx.createGain();
      o.type='sine'; o.frequency.value=880;
      g.gain.setValueAtTime(.0001,t0+off); g.gain.exponentialRampToValueAtTime(Math.max(.01,.35*vol),t0+off+.02); g.gain.exponentialRampToValueAtTime(.0001,t0+off+.17);
      o.connect(g); g.connect(ctx.destination); o.start(t0+off); o.stop(t0+off+.18);
      o.onended=()=>{ live=live.filter(x=>x!==o); try{g.disconnect()}catch(e){} };
      live.push(o); stats.beeps++;
    });
  }
  function start(){ if(playing) return; playing=true; stats.starts++; pattern(); timer=setInterval(pattern,3000); }
  function stop(){
    if(!playing && !live.length) return;
    if(playing) stats.stops++;
    playing=false; clearInterval(timer); timer=null;
    live.forEach(o=>{ try{o.onended=null;o.stop();o.disconnect();}catch(e){} }); live=[];
  }
  window.EVAudio = {
    supported: !!AC,
    async enable(){            // must run from a click/tap; produces silence
      if(!AC) return false;
      ctx = ctx || new AC();
      await ctx.resume();
      const b=ctx.createBuffer(1,1,22050), s=ctx.createBufferSource(); s.buffer=b; s.connect(ctx.destination); s.start(0);   // silent iOS unlock
      enabled=true; return true;
    },
    disable(){ enabled=false; stop(); },
    update(shouldPlay){ if(enabled && shouldPlay) start(); else stop(); },
    setVolume(v){ vol=Math.max(.05,Math.min(1,v)); },
    get enabled(){ return enabled; }, get playing(){ return playing; }, stats
  };
})();
