"""Safe ChatGPT upload chooser handling."""
import json
import os
import time

JS_PICK_FILE_INPUT = '((want)=>{\nconst all=Array.from(document.querySelectorAll(\'input[type="file"]\'));\nall.forEach(i=>i.removeAttribute(\'data-twl-file\'));\nconst composer=document.querySelector(\'#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea\');\nconst form=composer&&composer.closest(\'form\');\nconst compatible=i=>{\n if(i.disabled)return false;\n const a=(i.getAttribute(\'accept\')||\'\').toLowerCase().trim();\n if(want===\'image\')return !a||a.includes(\'image\')||a.includes(\'*/*\');\n if(!a||a.includes(\'text\')||a.includes(\'.txt\')||a.includes(\'*/*\'))return true;\n return !a.split(\',\').every(x=>x.trim().startsWith(\'image\'));\n};\nconst eligible=all.filter(compatible);\nconst near=eligible.filter(i=>form&&form.contains(i));\nlet best=near.length===1?near[0]:null;\nif(!best&&near.length>1){\n const broad=near.filter(i=>!(i.getAttribute(\'accept\')||\'\').trim()||(i.getAttribute(\'accept\')||\'\').includes(\'.txt\'));\n if(broad.length===1)best=broad[0];\n}\nif(!best&&near.length===0&&eligible.length===1)best=eligible[0];\nif(!best)return 0;\nbest.setAttribute(\'data-twl-file\',\'1\');return eligible.length;\n})(%s)'
JS_ATTACHED = '((name)=>{\nconst a=document.querySelector(\'#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea\');\nif(!a)return {blob:0,named:false,busy:false};\nconst visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!==\'hidden\';\nlet f=a.closest(\'form\')||a.parentElement;\nfor(let n=0;n<5&&f&&f!==document.body;n++,f=f.parentElement){\n if(f.querySelector(\'[data-message-author-role],[data-turn="user"],[data-turn="assistant"]\'))break;\n const txt=f.innerText||\'\';const stem=name.replace(/\\.[^.]+$/,\'\');\n const named=txt.includes(name)||(stem.length>3&&txt.includes(stem));\n const blob=Array.from(f.querySelectorAll(\'img\')).filter(i=>visible(i)&&(i.currentSrc||i.src||\'\').startsWith(\'blob:\')).length;\n const busy=Array.from(f.querySelectorAll(\'[role="progressbar"],circle[stroke-dasharray],.animate-spin,[aria-busy="true"]\')).some(visible);\n if(named||blob)return {blob,named,busy};\n}\nreturn {blob:0,named:false,busy:false};\n})(%s)'


# Inspect visible UI only, never messages or transcript content. Return no page text.
JS_QUOTA = r'''(()=>{
const visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
const patterns=[/file upload limit reached/i,/you(?:['’]ve| have) reached (?:your |the )?(?:file |upload |attachment )*(?:limit|quota)/i,
 /too many (?:file )?uploads/i,/(?:upload|attachment|file).{0,35}(?:limit|quota).{0,25}(?:reached|exceeded)/i,
 /(?:достигнут|исчерпан|превышен).{0,30}лимит.{0,30}(?:загруз|файл|вложен)/i,
 /лимит.{0,25}(?:загруз|файл|вложен).{0,25}(?:достигнут|исчерпан|превышен)/i,
 /вы (?:достигли|исчерпали).{0,30}лимит.{0,30}(?:загруз|файл|вложен)/i,
 /перейти на plus.{0,35}чтобы загружать больше/i,
 /подождите.{0,50}чтобы повторить загрузку/i,
 /upgrade to plus.{0,50}upload more/i,
 /(?:wait|try again).{0,60}(?:upload|attach).{0,30}(?:file|again)?/i];
for(const e of document.querySelectorAll('[role="alert"],[role="dialog"],[role="menu"],[role="menuitem"],[role="tooltip"],[data-testid*="toast"],button,p,span,div')){
 if(!visible(e)||e.closest('[data-message-author-role],[data-turn],pre,code,[contenteditable="true"],textarea'))continue;
 if(e.querySelector('[data-message-author-role],[data-turn],pre,code,[contenteditable="true"],textarea'))continue;
 const text=((e.innerText||'')+' '+(e.getAttribute('title')||'')+' '+(e.getAttribute('aria-label')||'')).trim();
 if(text.length<=400&&patterns.some(p=>p.test(text)))return {blocked:true};
}
return {blocked:false};})()
'''

# Only inspect and click visible composer/menu controls, never content in a chat message.
JS_OPEN_ATTACH = r'''(()=>{
const e=document.querySelector('#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea');
const form=e&&e.closest('form');
const visible=n=>!!n&&!!n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden';
const sel='button[data-testid="composer-plus-btn"],button[aria-label="Add files and more"],button[aria-label="Добавить файлы и другое"]';
let buttons=Array.from(document.querySelectorAll(sel)).filter(visible);
if(form){const near=buttons.filter(x=>form.contains(x));if(near.length)buttons=near;}
if(buttons.length!==1||buttons[0].disabled||buttons[0].getAttribute('aria-disabled')==='true')return false;
buttons[0].click();return true;})()'''

JS_CLICK_UPLOAD = r'''(()=>{
const visible=n=>!!n&&!!n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden';
const all=Array.from(document.querySelectorAll('[role="menuitem"], [role="menu"] button, [role="menu"] label'));
const items=all.filter(e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&
 /add.*(photo|file)|upload.*file|добавить.*(файл|фото)|загрузить.*файл/i.test((e.innerText||'')+' '+(e.getAttribute('aria-label')||'')));
if(items.length!==1)return false;
items[0].click();return true;})()'''

def attach(self, path: str) -> bool:
    tab=self.tab; name=os.path.basename(path)
    image=os.path.splitext(name)[1].lower() in ('.png','.jpg','.jpeg','.webp','.gif')
    self._check_upload_quota()
    # No native dialog: if interception cannot be enabled, do not click anything.
    try:
        tab.call('Page.setInterceptFileChooserDialog',enabled=True)
    except Exception:
        self._status('браузер не разрешил безопасно перехватить выбор файла')
        return False
    if hasattr(tab,'clear_file_chooser'): tab.clear_file_chooser()
    opened=False; menu_clicked=False; backend=None; nid=None
    self._status('ожидаю загрузку кнопки и меню прикрепления (до 20 с)')
    # Bounded attempts also work in tests with a mocked clock/sleep. Do not click
    # plus or the upload item twice: delayed events may still be in flight.
    deadline=time.monotonic()+self._remaining(20)
    for attempt in range(40):
        if time.monotonic()>=deadline or self._remaining(1)<=0:break
        self._check_upload_quota()
        if not opened:
            try: opened=bool(tab.js(JS_OPEN_ATTACH))
            except Exception:
                self._status('не удалось проверить кнопку загрузки; запрос не отправлен')
                return False
        if opened and hasattr(tab,'wait_file_chooser'):
            event=tab.wait_file_chooser(.1)
            backend=(event or {}).get('backendNodeId')
            if backend:break
        if opened and not menu_clicked:
            self._check_upload_quota()
            try:menu_clicked=bool(tab.js(JS_CLICK_UPLOAD))
            except Exception:
                self._status('не удалось выбрать пункт загрузки; запрос не отправлен')
                return False
            if menu_clicked:
                self._status('пункт загрузки выбран; ожидаю окно выбора файла')
                if hasattr(tab,'wait_file_chooser'):
                    event=tab.wait_file_chooser(.5)
                    backend=(event or {}).get('backendNodeId')
                    if backend:break
        # Alternate route: after opening the menu, allow it time to hydrate,
        # then use only a uniquely identified compatible file input. No upload
        # request has been made yet. Continue polling for late controls.
        if opened and attempt>=6:
            self._check_upload_quota()
            if attempt==6:self._status('проверяю запасной путь: поле загрузки страницы')
            try:
                n=tab.js(self.JS_PICK_FILE_INPUT % json.dumps('image' if image else 'any')) or 0
                nid=tab.node_id('input[data-twl-file="1"]') if n else None
            except Exception:nid=None
            if nid:break
        if attempt in (15,30):self._status('интерфейс загрузки ещё подготавливается; запрос не отправлен')
        time.sleep(max(0,min(.5,deadline-time.monotonic())))
    self._check_upload_quota()
    if backend:
        self._status('найдено поле загрузки, выбранное меню ChatGPT')
        tab.call('DOM.setFileInputFiles',files=[os.path.abspath(path)],backendNodeId=backend)
    elif nid:
        self._status('найдено поле загрузки страницы; использую запасной путь')
        tab.call('DOM.setFileInputFiles',files=[os.path.abspath(path)],nodeId=nid)
    else:
        self._status('не удалось найти однозначное поле загрузки за 20 с; запрос не отправлен')
        return False
    if self._wait_attached(name,image):
        self._status('прикрепление файла подтверждено')
        return True
    # Delivery to the page is uncertain: do not upload the same file again.
    self._status('не удалось подтвердить вложение; повторная загрузка не выполняется')
    return False
