const btn=document.querySelector('.menu');const nav=document.querySelector('.links');
if(btn&&nav){btn.addEventListener('click',()=>{const open=nav.classList.toggle('open');btn.setAttribute('aria-expanded',String(open));});nav.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{nav.classList.remove('open');btn.setAttribute('aria-expanded','false');}));}
const y=document.getElementById('year');if(y)y.textContent=new Date().getFullYear();
