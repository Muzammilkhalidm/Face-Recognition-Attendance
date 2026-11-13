/* CyberAttend motion: button ripples, animated counters, tilt-free cards. No dependencies. */
(function(){
  'use strict';
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  /* ripple on every button / button-like link */
  document.addEventListener('pointerdown',function(e){
    var b=e.target.closest&&e.target.closest('.btn,.theme-trigger,.theme-menu button,.nav-item');
    if(!b||reduce)return;
    var r=b.getBoundingClientRect(),s=document.createElement('span');
    s.className='ca-ripple';s.style.left=(e.clientX-r.left)+'px';s.style.top=(e.clientY-r.top)+'px';
    if(getComputedStyle(b).position==='static')b.style.position='relative';
    b.style.overflow='hidden';b.appendChild(s);setTimeout(function(){s.remove();},750);
  },{passive:true});
  /* count-up for big numbers in stat cards */
  function countUp(el){
    var txt=(el.textContent||'').trim();
    if(!/^\d[\d,]*$/.test(txt))return;
    var end=parseInt(txt.replace(/,/g,''),10);
    if(!isFinite(end)||end<2||end>1e7)return;
    var t0=null,dur=900;
    function step(ts){
      if(t0===null)t0=ts;
      var p=Math.min((ts-t0)/dur,1),v=Math.round(end*(1-Math.pow(1-p,3)));
      el.textContent=v.toLocaleString('en-US');
      if(p<1)requestAnimationFrame(step);else el.textContent=txt;
    }
    el.textContent='0';requestAnimationFrame(step);
  }
  function init(){
    setTimeout(function(){document.body.classList.add('ca-ready');},50);
    if(reduce)return;
    document.querySelectorAll('.stat-card .value,.grid-cards .stat-card b,[data-count]').forEach(countUp);
    /* reveal-on-scroll for cards far below the fold */
    if('IntersectionObserver' in window){
      var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting){en.target.classList.add('ca-in');io.unobserve(en.target);}});},{threshold:.08});
      document.querySelectorAll('.card').forEach(function(c){io.observe(c);});
    }
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
