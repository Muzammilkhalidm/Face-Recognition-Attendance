(function(){
  'use strict';
  var key='cyberattend-appearance';
  var allowed=['light','dark','blue','purple','red','gold'];
  function apply(theme){
    if(allowed.indexOf(theme)<0) theme='light';
    document.body.setAttribute('data-theme',theme);
    document.querySelectorAll('[data-theme-choice]').forEach(function(btn){
      btn.setAttribute('aria-pressed',String(btn.getAttribute('data-theme-choice')===theme));
    });
    try{localStorage.setItem(key,theme);}catch(e){}
  }
  var saved='light';
  try{saved=localStorage.getItem(key)||'light';}catch(e){}
  apply(saved);
  var trigger=document.querySelector('[data-theme-trigger]');
  var menu=document.querySelector('[data-theme-menu]');
  if(!trigger||!menu)return;
  trigger.addEventListener('click',function(){
    var opening=menu.hidden;menu.hidden=!opening;trigger.setAttribute('aria-expanded',String(opening));
  });
  menu.querySelectorAll('[data-theme-choice]').forEach(function(btn){
    btn.addEventListener('click',function(){apply(btn.getAttribute('data-theme-choice'));menu.hidden=true;trigger.setAttribute('aria-expanded','false');});
  });
  document.addEventListener('click',function(e){if(!e.target.closest('.theme-control')){menu.hidden=true;trigger.setAttribute('aria-expanded','false');}});
  document.addEventListener('keydown',function(e){if(e.key==='Escape'){menu.hidden=true;trigger.setAttribute('aria-expanded','false');}});
})();