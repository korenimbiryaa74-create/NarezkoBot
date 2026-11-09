"""Download pinned local FLUX weights after the user has accepted the licence."""
from __future__ import annotations
import hashlib
import sys
from pathlib import Path

REPO = 'black-forest-labs/flux-3-image-klein-comfy'
REVISION = '9d36114cf7a2e03a45c28f5cc4987d20ebfdb6c1'
FILES = {
 'diffusion_models/flux3-image-klein-int8-convrot.safetensors': '847d8933548afd93df4af2cac2498e70891bf28d9dae06d695f536992e168fe2',
 'text_encoders/qwen3vl_4b_int8_convrot.safetensors': '32de113cbf8282b02cbff0794b637b9d6676da09314a1db5a30bcea9bf4ee01c',
 'vae/flux2-vae.safetensors': '868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3',
 'text_encoders/qwen3.5_9b_flux3_prompt_upsampler_w4a8.safetensors': 'e5d85a7421d197bf5a12474511936ecabce19a833252f16e9310a983194a4458',
}

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def download(model_dir):
    from huggingface_hub import hf_hub_download
    model_dir=Path(model_dir)
    for name,expected in FILES.items():
        path=model_dir/name
        if path.exists():
            print('Проверка:',name,flush=True)
            if not path.is_file() or digest(path)!=expected:
                raise RuntimeError('Существующий файл отличается, он не перезаписан: '+str(path))
            continue
        print('Скачивание:',name,flush=True)
        got=Path(hf_hub_download(repo_id=REPO,filename=name,revision=REVISION,local_dir=str(model_dir)))
        if digest(got)!=expected:raise RuntimeError('Контрольная сумма файла не совпадает: '+str(got))
    print('Модели загружены и проверены.',flush=True)

if __name__=='__main__':
    try:download(sys.argv[1])
    except Exception as exc:
        print('Не удалось загрузить модель. Проверьте доступ к лицензии и вход в Hugging Face:',str(exc),file=sys.stderr)
        sys.exit(1)
