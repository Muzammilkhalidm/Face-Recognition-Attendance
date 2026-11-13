(function(){
  'use strict';
  var root=document.documentElement;
  var scene=document.querySelector('.auth-visual');
  var reduced=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(scene&&!reduced){
    root.classList.add('auth-motion-ready');
    if(window.matchMedia('(hover: hover) and (pointer: fine)').matches){
      var frame=0;
      scene.addEventListener('pointermove',function(e){
        var r=scene.getBoundingClientRect(),x=(e.clientX-r.left)/r.width-.5,y=(e.clientY-r.top)/r.height-.5;
        if(frame)cancelAnimationFrame(frame);
        frame=requestAnimationFrame(function(){scene.style.setProperty('--scene-tilt-x',(y*-3).toFixed(2)+'deg');scene.style.setProperty('--scene-tilt-y',(x*4).toFixed(2)+'deg');});
      },{passive:true});
      scene.addEventListener('pointerleave',function(){scene.style.removeProperty('--scene-tilt-x');scene.style.removeProperty('--scene-tilt-y');},{passive:true});
    }
  }
  document.querySelectorAll('.auth-card form').forEach(function(form){
    form.addEventListener('submit',function(){
      if(!form.checkValidity())return;
      var overlay=document.querySelector('.signin-transition');
      var button=form.querySelector('button[type="submit"]');
      if(button){button.disabled=true;button.setAttribute('aria-busy','true');}
      if(overlay){overlay.classList.add('is-visible');overlay.setAttribute('aria-hidden','false');}
    });
  });
})();
