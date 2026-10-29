# Dock artwork

`dock-icon.png` is the Babo mark (`webui/public/brand/babo_mark.svg`) rendered at 824 pixels, centred on a transparent 1024-pixel canvas.

The packaged helper loads this PNG through AppKit. It needs neither a generated application executable nor an icon compiler at installation or launch. Keep artwork changes independent of the WebUI brand asset and of gateway lifecycle policy.
