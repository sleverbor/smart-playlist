'use strict';
(() => {
  const locale=navigator.language||'en';
  const catalogue=window.ENGLISH;
  const numbers=new Intl.NumberFormat(locale);
  const plurals=new Intl.PluralRules('en');
  const interpolate=(text,values)=>text.replace(/\{(\w+)\}/g,(_,name)=>String(values[name]??'{'+name+'}'));
  const t=(key,values={})=>{const text=catalogue[key];if(typeof text!=='string')throw new Error('Missing translation: '+key);return interpolate(text,values);};
  const plural=(key,count)=>{const forms=catalogue[key];return interpolate(forms[plurals.select(count)]||forms.other,{count:numbers.format(count)});};
  window.I18n={t,plural,number:value=>numbers.format(value),locale};
  document.documentElement.lang='en';
  document.querySelectorAll('[data-i18n]').forEach(el=>el.textContent=t(el.dataset.i18n));
  for(const name of ['placeholder','aria-label'])document.querySelectorAll('[data-i18n-'+name+']').forEach(el=>el.setAttribute(name,t(el.getAttribute('data-i18n-'+name))));
})();
