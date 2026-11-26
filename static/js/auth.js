/* Login / register / first-run setup forms. Server re-validates everything. */
(function(){
  const csrf = document.querySelector('meta[name=csrf]').content;
  const f = document.getElementById('f'); if(!f) return;
  const msg = document.getElementById('msg');
  const $ = n => f.elements[n];
  function setErr(name, text){
    const el = f.querySelector('[data-err="'+name+'"]'), inp = $(name);
    if(el) el.textContent = text||''; if(inp) inp.setAttribute('aria-invalid', text?'true':'false');
  }
  function showMsg(t){ msg.textContent=t||''; msg.classList.toggle('hide', !t); }
  document.querySelectorAll('[data-toggle]').forEach(b=>b.addEventListener('click',()=>{
    const i = document.getElementById(b.dataset.toggle), show = i.type==='password';
    i.type = show?'text':'password'; b.setAttribute('aria-label', show?'Hide password':'Show password');
    b.firstElementChild.firstElementChild.setAttribute('href', show?'#i-eyeoff':'#i-eye');
  }));
  const meter = document.getElementById('meter');
  function strength(p){ let s=0; if(p.length>=10)s++; if(/[A-Za-z]/.test(p)&&/\d/.test(p))s++; if(p.length>=14)s++; if(/[^A-Za-z0-9]/.test(p)&&p.length>=10)s++; return s; }
  if(meter && $('password')) $('password').addEventListener('input',()=>{
    const s=strength($('password').value), col=['#ff5560','#ffb12b','#b7e04b','#38d996'][Math.max(0,s-1)];
    [...meter.children].forEach((c,i)=>c.style.background = i<s?col:'');
  });
  function clientCheck(){
    let ok=true; const v=n=>($(n)&&$(n).value||'').trim();
    const need=(n,t)=>{ if(!v(n)){setErr(n,t);ok=false;} else setErr(n,''); };
    if(f.dataset.api.endsWith('login')){ need('identifier','Enter your username or email.'); if(!$('password').value){setErr('password','Enter your password.');ok=false;} else setErr('password',''); return ok; }
    need('full_name','Enter your name.');
    if(!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(v('email'))){setErr('email','Enter a valid email address.');ok=false;} else setErr('email','');
    if(!/^[A-Za-z0-9_.-]{3,32}$/.test(v('username'))){setErr('username','3-32 characters: letters, numbers, dot, dash or underscore.');ok=false;} else setErr('username','');
    const p=$('password').value;
    if(p.length<10||!/[A-Za-z]/.test(p)||!/\d/.test(p)){setErr('password','Use at least 10 characters with a letter and a number.');ok=false;} else setErr('password','');
    if($('confirm').value!==p){setErr('confirm','Passwords do not match.');ok=false;} else setErr('confirm','');
    return ok;
  }
  f.addEventListener('submit', async e=>{
    e.preventDefault(); showMsg('');
    if(!clientCheck()) return;
    const btn=f.querySelector('button[type=submit]'); btn.disabled=true;
    const body={}; [...f.elements].forEach(el=>{ if(el.name) body[el.name]= el.type==='checkbox'?el.checked:el.value; });
    try{
      const r = await fetch(f.dataset.api,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body),credentials:'same-origin'});
      const d = await r.json().catch(()=>({}));
      if(r.ok&&d.ok){ location.href=d.next||'/app'; return; }
      if(r.status===403&&d.error==='csrf'){ location.reload(); return; }
      if(d.errors) Object.keys(d.errors).forEach(k=>setErr(k,d.errors[k]));
      showMsg(d.msg||'Something went wrong. Try again.');
    }catch(err){ showMsg('Cannot reach the server. Check that the app is still running.'); }
    btn.disabled=false;
  });
})();
