# LUMI — adaptação do LCD-show para operação sem servidor gráfico

## Missão para o Codex

Trabalhe no clone local de https://github.com/fexdelux/LCD-show. Implemente, teste e documente um instalador mínimo do display SPI touchscreen de 3,5 polegadas para o projeto pessoal LUMI. Não apenas proponha código: faça as alterações no repositório local. Preserve alterações preexistentes do usuário; não faça push nem execute instalação no computador de desenvolvimento.

## Contexto e restrições

- Raspberry Pi 3, 1 GB RAM, Raspberry Pi OS Lite ARM64, MicroSD 16 GB.
- LUMI utiliza OpenVoiceOS, Ollama e voz offline; preservar RAM, CPU, GPIO e áudio. Não instalar esses serviços neste repositório.
- Display presumido LCD35/tft35a, 480 × 320, touchscreen resistivo; confirmar pelo código/overlay e pelo equipamento quando disponível. Não presumir que todo LCD de 3,5 polegadas seja igual.
- Renderização direta por framebuffer Linux e Pillow; touchscreen via evdev. Sem X11, Wayland, desktop, fbturbo, fbcp, compositor ou navegador.
- Meta indicativa do serviço gráfico: 15–30 MiB de RAM; medir no hardware, não declarar atingida sem evidência.

## 1. Auditoria inicial obrigatória

1. Registre `git status`, branch, commit e estrutura do clone. Leia README, LCD35-show, system_config.sh, system_backup.sh, system_restore.sh, rotate.sh, boot/, usr/ e etc/.
2. O LCD35-show público copia `usr/tft35a-overlay.dtb`, ativa SPI e usa `dtoverlay=tft35a:rotate=90`; também modifica Xorg, HDMI, inittab, rc.local, instala fbcp e pacotes X11 e reinicia. O system_config.sh altera comportamento de boot/Wayland e substitui configuração de boot por templates. Não reutilize esses fluxos no novo instalador.
3. Inspecione o overlay com dtc quando disponível; identifique drivers, compatible, SPI, GPIO, IRQ, touchscreen, frequência e parâmetros de rotação. Registre os achados. Não invente DTS ausente nem altere pinagem sem justificativa.
4. Verifique compatibilidade do overlay com kernel alvo (fbtft/driver correspondente e touchscreen). Diferencie evidência estática de teste real. Se o kernel não suportar o overlay, diagnostique e documente alternativa baseada em driver existente; não instale kernel antigo nem desabilite KMS automaticamente.

## 2. Entregáveis e organização

Preserve instaladores legados e licença/atribuições existentes. Adicione, ajustando a estrutura ao clone se necessário:

```text
LCD35-lite-show          # instalador mínimo
LCD35-lite-restore       # reversão segura
LCD35-lite-status        # diagnóstico somente leitura
lumi-lcd/
  framebuffer.py         # renderizador direto
  touch.py               # eventos e calibração
  demo.py                # demonstração opcional
  README.md
  systemd/lumi-display-demo.service  # opcional, não habilitar por padrão
  tests/
README.md                # seção LUMI, sem apagar documentação original
```

Não faça remoção global de arquivos legados. O novo caminho não deve depender deles, exceto do overlay e outros ativos de hardware comprovadamente necessários.

## 3. Instalador

- Bash com `set -Eeuo pipefail`, caminhos relativos à localização do script, validação de argumentos e mensagens úteis. Exigir root somente para mudanças reais. Suportar `--help`, `--dry-run`, `--rotate 0|90|180|270` (somente valores suportados pelo overlay), `--boot-dir PATH` para testes e imagens montadas, `--install-demo` opcional. Sem reboot automático.
- Detectar Raspberry Pi 3, arquitetura `aarch64`/`arm64`, versão do SO e kernel; emitir diagnóstico claro para ambiente não suportado. Em testes offline usar fixtures explícitas, nunca fingir que é um Pi.
- Resolver `/boot/firmware/config.txt` e overlays modernos, com fallback para `/boot/config.txt` e `/boot/overlays`. Validar caminhos e evitar seguir symlinks inesperados; não criar symlink legado.
- Verificar overlay existente. Se o overlay do sistema for compatível, preferir reutilizá-lo; caso contrário instalar o ativo do repo como `.dtbo` após validar. Não sobrescrever arquivo diferente sem autorização explícita e backup.
- Adicionar bloco gerenciado na seção `[all]` efetiva, preservando seções e configurações existentes:

```ini
# BEGIN LUMI LCD35
[all]
dtparam=spi=on
dtoverlay=tft35a:rotate=90
# END LUMI LCD35
```

Tratar a semântica das seções condicionais: posicionar o bloco em local seguro, sem mudar o escopo de linhas alheias; detectar configurações duplicadas/conflitantes e abortar com orientação quando não for seguro reconciliar. Evitar duplicatas em reinstalação. Validar sintaxe antes de substituir atomicamente. Não alterar HDMI, UART, I2C, cmdline, KMS, boot desktop, Xorg, rc.local ou perfis sem necessidade comprovada e decisão explícita.
- Backup persistente com manifesto de caminhos, hashes, permissões e indicação de arquivos criados pelo instalador. Preservar backup original em reinstalações. Usar lock e rollback em falha parcial; não deixar alterações de boot incompletas.
- Instalação base não deve exigir rede, compilar fbcp nem instalar dependências gráficas. Demo opcional pode usar apt para `python3-pil`, `python3-evdev`, `fbset` e ferramentas de diagnóstico se disponíveis. Mostrar plano antes de instalar; evitar pacotes recomendados desnecessários e não atualizar todo o sistema.
- Modo imagem/offline: alterar somente a raiz/boot explicitamente indicada; não executar apt, systemctl, reboot ou modificar o host. Se não implementar instalação completa em imagem, documentar o limite.

## 4. Diagnóstico e restauração

`LCD35-lite-status`: mostrar kernel, arquitetura, boot config, overlay, `/proc/fb`, `/dev/fb*`, sysfs de framebuffer e input, módulos/logs relevantes (permissões podem limitar dmesg), formato, resolução e stride quando disponíveis. Detectar display SPI sem assumir `/dev/fb1`; indicar ambiguidades e permitir seleção explícita. Informar ausência de driver com próximos passos. Não alterar sistema.

`LCD35-lite-restore`: oferecer dry-run, verificar manifesto/hashes, remover apenas bloco/arquivos próprios, preservar mudanças posteriores do usuário e abortar se houver conflito que exija decisão manual. Restaurar overlay anterior somente se substituído pelo instalador. Não apagar configurações alheias nem reiniciar automaticamente.

## 5. Biblioteca gráfica e demonstração

- Python 3, Pillow para renderizar; framebuffer via mmap ou escrita controlada. Consultar `FBIOGET_FSCREENINFO` e `FBIOGET_VSCREENINFO` (ou dados equivalentes confiáveis) para resolução, stride, offsets, bits por pixel e bitfields de cor. Implementar RGB565 e testar endianness; rejeitar formatos não suportados com erro claro. Não presumir stride = largura × 2; respeitar padding e limites do buffer. Permitir `--fb /dev/fbN` e seleção automática apenas quando inequívoca.
- Demo simples: nome LUMI, estados aguardando/escutando/processando/falando, cores e texto legíveis; atualização por evento, sem busy loop, animação opcional limitada a 5–10 FPS. Não integrar OVOS real agora; documentar interface futura para receber eventos do Message Bus. Liberar recursos em SIGTERM.
- Touchscreen: usar evdev, identificar dispositivo pelas capacidades ABS/BTN e nome, aceitar caminho explícito, ler min/max, tratar inversão/troca de eixos e rotação; prever calibração persistente com teste de quatro cantos e centro. Não depender de xinput-calibrator.
- Permissões: evitar chmod 777 e execução do demo como root. Documentar grupos/permissões restritas para framebuffer e input; considerar que grupo input pode conceder acesso a outros dispositivos. Serviço systemd opcional com usuário dedicado, reinício limitado e logs moderados; não habilitar automaticamente.

## 6. Testes e critérios de aceitação

No computador de desenvolvimento, executar somente testes seguros, sem privilégios nem alterações no host:

- `bash -n` nos scripts; ShellCheck se disponível; testes com fixtures/tempdir para boot moderno e legado, config com seções, repetição, conflito, backup, rollback, restore, dry-run, caminhos com espaços e ausência de overlay.
- Testes Python para conversão de cores (vermelho/verde/azul), stride com padding, limites, rotação e calibração touch. Simular framebuffer em arquivo/buffer, sem depender do Pi. Testar erro para formato não suportado.
- Demonstrar que o fluxo novo não invoca X11, Wayland, fbcp, raspi-config de desktop nem reboot. Validar ausência de dependências gráficas obrigatórias.
- Se não houver Raspberry conectado, marcar testes físicos como PENDENTES; não afirmar compatibilidade comprovada.

No Raspberry Pi 3, após revisão humana:

```bash
sudo ./LCD35-lite-show --dry-run
sudo ./LCD35-lite-show --rotate 90
sudo reboot
./LCD35-lite-status
cat /proc/fb
ls -l /dev/fb* /dev/input/event*
free -h
```

Documentar instalação opcional e comando real para rodar demo conforme implementação. Validar imagem visível, cores corretas, orientação, quatro cantos touch, reinicialização, SSH preservado, ausência de Xorg/Wayland/fbcp em execução, restore e reinstalação. Medir RSS/PSS do demo, CPU em repouso, FPS, `vmstat 1`, temperatura e memória disponível. Não usar swap como substituto de benchmark. Verificar conflito GPIO com periféricos futuros; microfone USB não deve exigir GPIO do LCD.

## 7. Documentação e conclusão

README em português claro: requisitos, instalação, dry-run, diagnóstico, demo, calibração, restauração, compatibilidade por SO/kernel/modelo, limites conhecidos, solução de tela branca, ausência de framebuffer e touch invertido. Explicar que o driver do kernel é independente de X11, mas compatibilidade do overlay antigo com kernel moderno deve ser demonstrada. Incluir como incorporar instalador ao futuro LUMI OS sem credenciais ou caminhos pessoais.

Ao concluir, apresentar resumo dos arquivos modificados, decisões técnicas, comandos exatos, testes executados e resultados, pendências físicas, riscos e `git diff --stat`. Não criar commit nem push sem pedido. Se encontrar incompatibilidade não resolvida, entregue o diagnóstico e as partes implementadas, sem mascarar falhas.

## Referências do repositório

- https://github.com/fexdelux/LCD-show
- https://github.com/fexdelux/LCD-show/blob/master/LCD35-show
- https://github.com/fexdelux/LCD-show/blob/master/system_config.sh

A versão local é a fonte de verdade; verificar diferenças antes de editar.
