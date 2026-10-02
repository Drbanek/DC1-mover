let appLang="cs";
const I18N={
cs:{dashboard:"Dashboard",migrations:"Migrace",dns:"DNS",settings:"Nastavení",users:"Uživatelé a oprávnění",newUser:"+ Nový uživatel",user:"Uživatel",password:"Heslo",language:"Jazyk",cancel:"Zrušit",save:"Uložit"},
en:{dashboard:"Dashboard",migrations:"Migrations",dns:"DNS",settings:"Settings",users:"Users and permissions",newUser:"+ New user",user:"Username",password:"Password",language:"Language",cancel:"Cancel",save:"Save"}
};
function t(k){return (I18N[appLang]&&I18N[appLang][k])||I18N.cs[k]||k}
function applyLanguage(lang){appLang=lang==="en"?"en":"cs";document.documentElement.lang=appLang;document.querySelectorAll("[data-i18n]").forEach(el=>{const v=t(el.dataset.i18n);if(el.childNodes.length===1&&el.firstChild.nodeType===3)el.textContent=v;else{for(const n of el.childNodes){if(n.nodeType===3){n.textContent=v;break}}}});const map={tabButtonDashboard:"dashboard",tabButtonMigrations:"migrations",tabButtonDns:"dns",tabButtonSettings:"settings"};Object.entries(map).forEach(([id,k])=>{const e=document.getElementById(id);if(e)e.textContent=t(k)})}
