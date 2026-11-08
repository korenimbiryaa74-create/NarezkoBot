"""Safe ChatGPT upload chooser handling."""
import json
import os
import time

JS_PICK_FILE_INPUT = '((want)=>{\nconst all=Array.from(document.querySelectorAll(\'input[type="file"]\'));\nall.forEach(i=>i.removeAttribute(\'data-twl-file\'));\nconst composer=document.querySelector(\'#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea\');\nconst form=composer&&composer.closest(\'form\');\nconst compatible=i=>{\n if(i.disabled)return false;\n const a=(i.getAttribute(\'accept\')||\'\').toLowerCase().trim();\n if(want===\'image\')return !a||a.includes(\'image\')||a.includes(\'*/*\');\n if(!a||a.includes(\'text\')||a.includes(\'.txt\')||a.includes(\'*/*\'))return true;\n return !a.split(\',\').every(x=>x.trim().startsWith(\'image\'));\n};\nconst eligible=all.filter(compatible);\nconst near=eligible.filter(i=>form&&form.contains(i));\nlet best=near.length===1?near[0]:null;\nif(!best&&near.length>1){\n const broad=near.filter(i=>!(i.getAttribute(\'accept\')||\'\').trim()||(i.getAttribute(\'accept\')||\'\').includes(\'.txt\'));\n if(broad.length===1)best=broad[0];\n}\nif(!best&&near.length===0&&eligible.length===1)best=eligible[0];\nif(!best)return 0;\nbest.setAttribute(\'data-twl-file\',\'1\');return eligible.length;\n})(%s)'
JS_ATTACHED = '((name)=>{\nconst a=document.querySelector(\'#prompt-textarea,div.ProseMirror[contenteditable=true],form textarea\');\nif(!a)return {blob:0,named:false,busy:false};\nconst visible=e=>!!e&&!!e.getClientRects().length&&getComputedStyle(e).visibility!==\'hidden\';\nlet f=a.closest(\'form\')||a.parentElement;\nfor(let n=0;n<5&&f&&f!==document.body;n++,f=f.parentElement){\n if(f.querySelector(\'[data-message-author-role],[data-turn="user"],[data-turn="assistant"]\'))break;\n const txt=f.innerText||\'\';const stem=name.replace(/\\.[^.]+$/,\'\');\n const named=txt.includes(name)||(stem.length>3&&txt.includes(stem));\n const blob=Array.from(f.querySelectorAll(\'img\')).filter(i=>visible(i)&&(i.currentSrc||i.src||\'\').startsWith(\'blob:\')).length;\n const busy=Array.from(f.querySelectorAll(\'[role="progressbar"],circle[stroke-dasharray],.animate-spin,[aria-busy="true"]\')).some(visible);\n if(named||blob)return {blob,named,busy};\n}\nreturn {blob:0,named:false,busy:false};\n})(%s)'

def attach(self, path: str) -> bool:
    tab=self.tab; name=os.path.basename(path)
    image=os.path.splitext(name)[1].lower() in ('.png','.jpg','.jpeg','.webp','.gif')
    self._check_upload_quota()
    # Intercept the actual chooser to use its backend node, rather than an
    # unrelated hidden file input elsewhere in the page.
    try:
        tab.call('Page.setInterceptFileChooserDialog',enabled=True)
    except Exception:
        self._status('браузер не разрешил безопасно перехватить выбор файла')
        return False
    if hasattr(tab,'clear_file_chooser'): tab.clear_file_chooser()
    opened=False
    try:
        opened=bool(tab.js("""(()=>{const b=document.querySelector('button[data-testid="composer-plus-btn"],button[aria-label="Add files and more"],button[aria-label="Добавить файлы и другое"]');if(!b)return false;b.click();return true;})()"""))
    except Exception: pass
    event=None
    if opened and hasattr(tab,'wait_file_chooser'):
        event=tab.wait_file_chooser(.5)
    if opened and not event:
        menu_clicked=False
        for _ in range(8):
            self._check_upload_quota()
            try:
                menu_clicked=bool(tab.js("""(()=>{const item=Array.from(document.querySelectorAll('[role="menuitem"]')).find(e=>/Add photos.*files|Upload.*file|Добавить.*файл|Загрузить.*файл/i.test(e.innerText||''));if(!item)return false;item.click();return true;})()"""))
            except Exception: break
            if menu_clicked: break
            time.sleep(.5)
        if menu_clicked and hasattr(tab,'wait_file_chooser'):
            event=tab.wait_file_chooser(3)
    backend=(event or {}).get('backendNodeId')
    if backend:
        self._status('найдено поле загрузки, выбранное меню ChatGPT')
        tab.call('DOM.setFileInputFiles',files=[os.path.abspath(path)],backendNodeId=backend)
    else:
        self._status('поле загрузки через меню не найдено; проверяю поля страницы')
        try:
            n=tab.js(self.JS_PICK_FILE_INPUT % json.dumps('image' if image else 'any')) or 0
            nid=tab.node_id('input[data-twl-file="1"]') if n else None
        except Exception: nid=None
        if not nid:
            self._status('не удалось однозначно выбрать поле загрузки; запрос не отправлен')
            return False
        tab.call('DOM.setFileInputFiles',files=[os.path.abspath(path)],nodeId=nid)
    if self._wait_attached(name,image):
        self._status('прикрепление файла подтверждено')
        return True
    # Delivery to the page is uncertain: do not upload the same file again.
    self._status('не удалось подтвердить вложение; повторная загрузка не выполняется')
    return False

