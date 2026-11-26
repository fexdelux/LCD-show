# LUMI no LCD35 SPI

Renderização direta no framebuffer Linux, com Pillow na demonstração e evdev para o touch resistivo. O driver do painel funciona no kernel, independentemente de X11 ou Wayland. O alvo primário é Raspberry Pi 3 B/B+/A+, Raspberry Pi OS Lite ARM64 e painel LCD35/MPI3501 de 480 × 320 com ILI9486 e ADS7846 ou equivalente compatível. Confirme a placa e a revisão antes de conectar. Outras telas de 3,5″ podem usar interfaces e pinagens diferentes.

## Auditoria e compatibilidade

A auditoria partiu de `feature/lumi-project`, commit `a36c00a` (`modify hdmi shell`). O clone continha os scripts legados na raiz, templates em `boot/`, configurações em `etc/` e overlays, utilitários e arquivos de calibração em `usr/`. `LUMI-LCD-CODEX.md` e seu arquivo `Zone.Identifier` já estavam sem rastreamento. O fluxo de `LCD35-show` passa por backup/restauração legados, troca de templates, ajustes de HDMI, UART e I²C, configuração de Xorg/Wayland, fbcp, calibração X11 e reinicialização. Os comandos Lite usam apenas o ativo do painel.

O binário `usr/tft35a-overlay.dtb` foi inspecionado como FDT, pois `dtc` estava indisponível. Para repetir com a ferramenta, use `dtc -I dtb -O dts usr/tft35a-overlay.dtb`. SHA-256 `601ea7056da5d7864648798fd3656b4205f01d6b9a6a8a5cfad6ca5601bbbe1e`.

| Item | Evidência no binário |
| --- | --- |
| SoCs declarados | `brcm,bcm2835`, `brcm,bcm2708`, `brcm,bcm2709` |
| Painel | `ilitek,ili9486`, SPI0 CE0, 16 MHz, reset BCM25 ativo baixo, D/C BCM24 |
| Touch | `ti,ads7846`, SPI0 CE1, 2 MHz, PENIRQ BCM17 (borda de descida, ativo baixo) |
| SPI0 | Fixups para `spi0` e `gpio`, desativa ambos os nós spidev |
| Parâmetros | `rotate` padrão 90, `speed`, `txbuflen` 32768, `fps` 30, `bgr`, `debug` e `swapxy` para o touch |

O driver `fb_ili9486` registra o mesmo compatible e implementa 0°, 90°, 180° e 270°. A instalação no Pi verifica a presença de `fb_ili9486`, `ads7846` e `spi_bcm2835` por metadados do kernel, sysfs ou módulos. O defconfig ARM64 de referência da árvore Raspberry Pi 6.12 habilita os três como módulos. Essa evidência é estática. A aplicação dos fixups pelo firmware, a vinculação e o funcionamento no painel ainda precisam ser validados. [Driver Raspberry Pi Linux](https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/staging/fbtft/fb_ili9486.c) · [Defconfig ARM64 6.12](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm64/configs/bcm2711_defconfig)

| Ambiente | Situação |
| --- | --- |
| Pi 3, Raspberry Pi OS Lite ARM64 com layout moderno `/boot/firmware` | Alvo. Localização e planejamento testados em fixture. Firmware, kernel da imagem e painel pendentes. |
| Pi 3, Raspberry Pi OS Lite ARM64 com layout legado `/boot` | Fallback implementado e testado em fixture. Validação física pendente. |
| ARM32, outros modelos ou kernel sem os módulos necessários | Instalação local bloqueada. O modo imagem exige verificação independente do alvo. |

## Instalar e diagnosticar

Requer Bash e Python 3.9+ no alvo. Confira o modelo, a versão e o plano antes de gravar:

```sh
./LCD35-lite-status
sudo ./LCD35-lite-show --dry-run
sudo ./LCD35-lite-show --rotate 90
sudo reboot
```

Após reiniciar:

```sh
./LCD35-lite-status
cat /proc/fb
ls -l /dev/fb* /dev/input/event*
free -h
```

O instalador acrescenta ao fim de `config.txt` um bloco entre os marcadores LUMI. O filtro `[all]` limpa os filtros para o bloco depois da última linha do usuário. Um `dtparam=spi=on` global já existente é reaproveitado. Se houver um overlay anterior cujo escopo de parâmetros possa estar aberto, o bloco carrega o tft35a, fecha o escopo com `dtoverlay=` e só então configura o SPI na árvore base. O tft35a também habilita SPI0 diretamente. SPI desligado, condicional ou duplicado, overlays conflitantes conhecidos, includes, caminhos de overlay personalizados, árvore de dispositivos explícita e diretivas `gpio=` exigem revisão manual. Overlays personalizados e HATs podem reservar o mesmo SPI ou GPIO sem aparecer nessas verificações. Revise a pinagem antes de gravar. [Semântica do config.txt](https://www.raspberrypi.com/documentation/computers/config_txt.html)

Um `tft35a.dtbo` existente com o mesmo SHA-256 é reutilizado. Um binário diferente exige revisão e backup externos. A instalação base funciona sem rede. Não há reinicialização automática. `--boot-dir` aponta para uma partição explícita que já contém `config.txt` e `overlays/`:

```sh
./LCD35-lite-show --boot-dir "/mnt/imagem LUMI/boot" --dry-run
sudo ./LCD35-lite-show --boot-dir "/mnt/imagem LUMI/boot" --rotate 90
./LCD35-lite-status --boot-dir "/mnt/imagem LUMI/boot"
```

Prepare e monte a imagem fora do instalador, de preferência com acesso de escrita exclusivo durante a operação. O modo imagem configura apenas a partição de boot e não consulta o kernel da imagem, provisiona a raiz, executa apt ou registra serviços. Para incorporar no LUMI OS, inclua os comandos, o diretório `lumi-lcd/` e o overlay em suas posições relativas e execute primeiro o dry-run no boot montado. Verifique separadamente os módulos do kernel da imagem e instale as dependências da aplicação na imagem somente se ela usar a demo.

### Se o painel não responder

- **Tela branca:** com o Pi desligado, confirme modelo, encaixe e pinagem. Após o boot, confira o bloco e o hash, e procure SPI0/CE0 e CE1 no status. Backlight aceso por si só não indica que o driver iniciou o painel. Verifique erros de SPI, GPIO e inicialização no log. Use `sudo ./LCD35-lite-status` se o acesso ao dmesg for restrito.
- **Sem framebuffer:** confira o vínculo de `spi0.0` e `/proc/fb`. No kernel alvo, procure `CONFIG_FB_TFT_ILI9486`, `CONFIG_TOUCHSCREEN_ADS7846` e `CONFIG_SPI_BCM2835` no arquivo de configuração disponibilizado pelo sistema ou com `modinfo fb_ili9486` e `modinfo ads7846`. KMS/HDMI podem criar outro framebuffer. Compare nomes, barramento e formato no status; use `--fb /dev/fbN` após identificar o SPI. Um formato diferente de RGB565 é diagnosticado pelo status e rejeitado pelo renderizador.
- **Driver fbtft indisponível:** para hardware comprovadamente compatível com PiScreen, avalie o overlay oficial `piscreen` com o parâmetro `drm` (quando disponível no kernel/firmware da imagem) e o driver DRM ILI9486. Faça o teste separadamente após a reversão deste instalador. O PiScreen tem sequência de inicialização diferente e inclui BCM22 para backlight. Compare a fiação, os parâmetros e a posição das telas antes de usar. Verifique a emulação fbdev e seus bitfields. Se expuser XRGB8888, a demo atual precisará de um renderizador para esse formato ou de um cliente DRM. [Overlay PiScreen](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm/boot/dts/overlays/piscreen-overlay.dts) · [Driver DRM ILI9486](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/tiny/ili9486.c)
- **Touch ausente ou invertido:** confira `spi0.1` e o evento com ABS_X, ABS_Y e BTN_TOUCH. Selecione o dispositivo explicitamente e calibre após qualquer alteração de rotação. Confirme que o usuário tem acesso ao dispositivo.

## Demo e calibração

Instale dependências opcionais no Pi com `sudo ./LCD35-lite-show --rotate 90 --install-demo` ou, se o boot já estiver configurado, com `sudo apt-get install --no-install-recommends python3-pil python3-evdev fbset`. A opção do instalador mostra o plano e executa apt antes de gravar o boot. Em caso de falha subsequente, o rollback do boot não remove pacotes já instalados pelo apt.

Execute como usuário comum com acesso de leitura e gravação ao framebuffer SPI. Em muitas imagens, `video` dá acesso ao framebuffer. Confira as permissões reais com `stat /dev/fbN`. Para o touch, prefira regra udev local que atribua `MODE="0660"` e um grupo dedicado ao evento ADS7846 identificado por nome e caminho físico (`udevadm info -a -n /dev/input/eventN`). O grupo global `input` pode expor outros dispositivos, inclusive teclados. Para restringir o framebuffer também a um grupo dedicado, use uma regra equivalente com a identidade do painel SPI. Em ambos os casos, confira o resultado após reconectar ou reiniciar e abra sessão nova após mudanças de grupo.

```sh
python3 lumi-lcd/demo.py --without-touch --state aguardando
mkdir -p "$HOME/.config/lumi"
python3 lumi-lcd/demo.py --rotate 90 --calibrate --calibration "$HOME/.config/lumi/touch.json"
python3 lumi-lcd/demo.py --rotate 90 --calibration "$HOME/.config/lumi/touch.json"
```

Use `--fb /dev/fbN` e `--touch /dev/input/eventN` se houver ambiguidade. Para o touch é possível indicar um link estável. A calibração pede toques próximos aos quatro cantos, em sentido horário a partir do superior esquerdo, e depois no centro. O quarto canto e o centro verificam a tolerância da transformação. Ao reabrir a demo, toque nos cinco pontos e compare com o cursor exibido. Sem calibração, é possível experimentar `--swap-axes`, `--invert-x`, `--invert-y` e `--rotate`. A rotação da demo documenta a orientação aplicada pelo overlay e ajusta o touch sem calibração.

Cada toque alterna entre aguardando, escutando, processando e falando. A demo bloqueia enquanto aguarda eventos e encerra via SIGTERM. Para integração futura, um adaptador do Message Bus OVOS pode traduzir eventos de sessão para essas quatro chaves, enviá-las à mesma fila de desenho e ignorar estados repetidos. Mantenha uma única rotina escrevendo no framebuffer.

A unidade opcional em `systemd/lumi-display-demo.service` usa `lumi-display` e inicia em modo sem touch. Se quiser testá-la, crie o usuário com `sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin lumi-display`, copie `demo.py`, `framebuffer.py` e `touch.py` para `/opt/lumi-lcd/` com proprietário root e permissões de leitura, e copie a unidade para `/etc/systemd/system/`. Após `sudo systemctl daemon-reload`, use `sudo systemctl start lumi-display-demo` e consulte `journalctl -u lumi-display-demo`. Ajuste o grupo ou o parâmetro `--fb` conforme o status do Pi. A unidade limita tentativas de reinício e permanece desabilitada até habilitação explícita.

## Restaurar

```sh
./LCD35-lite-restore --dry-run
sudo ./LCD35-lite-restore
sudo reboot
```

O manifesto `.lumi-lcd35.json` no boot guarda caminhos, cópia original da configuração, hashes, UID/GID, permissões e a indicação de overlay criado. Em FAT, proprietário e permissões seguem as opções da montagem. O backup original permanece durante as reinstalações e é removido após uma restauração concluída. Arquive-o externamente se quiser retê-lo. A restauração remove o bloco intacto no final e conserva alterações feitas antes dele. Se houver linhas após o bloco, alterações no bloco, no overlay ou indícios de conflito, revise manualmente. Em particular, linhas acrescentadas após `[all]` podem depender desse filtro. Só remova o bloco após confirmar o escopo desejado.

O lock serializa comandos Lite sobre o mesmo diretório de boot. As substituições são atômicas por arquivo. O diário `.lumi-lcd35-pending.json` registra estados anteriores e propostos para rollback em falha parcial. Se houver interrupção abrupta, examine o diário antes de reiniciar e execute `restore` para recuperar a transação e então restaurar a instalação. A recuperação para se encontrar arquivos diferentes dos dois estados registrados. Em cartões FAT, perda de energia ou corrupção do sistema de arquivos ainda pode exigir recuperação externa. Mantenha uma cópia do cartão antes da primeira instalação.

## Testes e validação no Pi 3

```sh
for script in LCD35-lite-show LCD35-lite-restore LCD35-lite-status; do bash -n "$script" || break; done
PYTHONPATH=lumi-lcd python3 -B -m unittest discover -s lumi-lcd/tests -v
```

Para verificar as cores primárias sem o touch, desenhe três faixas no framebuffer SPI já identificado:

```sh
PYTHONPATH=lumi-lcd python3 -B - <<'PYCODE'
from PIL import Image
from framebuffer import Framebuffer

with Framebuffer() as display:
    w, h = display.layout.width, display.layout.height
    image = Image.new("RGB", (w, h))
    for i, rgb in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255))):
        image.paste(rgb, (w * i // 3, 0, w * (i + 1) // 3, h))
    display.draw(image)
PYCODE
```

No Pi, após revisão:

1. Instale com o plano, reinicie manualmente e reconecte via SSH. Confirme SPI, touch e áudio. Verifique a ausência de processos Xorg, Wayland e fbcp com `ps -eo comm,args`. Confirme que um microfone USB aparece como dispositivo de áudio e revise conflitos com SPI0 (BCM7–11) e BCM17/24/25 antes de ligar outros periféricos.
2. Rode a demo e confirme orientação, legibilidade, cores primárias e os quatro estados. Calibre, teste quatro cantos e centro, e reinicie para verificar persistência. Confirme que o serviço opcional termina com SIGTERM. Valide restauração e reinstalação.
3. Meça com a demo em repouso e durante toques, primeiro isolada e depois com a carga esperada do LUMI. Use `pid=$(pgrep -n -f '[p]ython3 .*demo.py')`, `ps -p "$pid" -o rss,%cpu,etime,args`, `awk '/^(Rss|Pss):/' /proc/"$pid"/smaps_rollup`, `top -b -p "$pid" -d 1 -n 10`, `vmstat 1 10`, `free -h` e `cat /sys/class/thermal/thermal_zone0/temp` (milésimos de °C). Para FPS efetivo, filme uma sequência de toques e conte as mudanças de quadro visíveis por segundo. A demo não anima e o esperado em repouso é zero atualizações de aplicação. Compare RSS/PSS com a meta indicativa de 15–30 MiB. Registre as condições e o uso de swap ao lado dos resultados.
