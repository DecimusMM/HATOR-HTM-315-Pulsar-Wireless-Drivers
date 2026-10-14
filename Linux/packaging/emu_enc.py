import os, struct, sys, pefile
"""Run the vendor button encoder (OemDrv.exe 0x416580, CDev8948 vtable+0x10) under Unicorn.
Usage: python3 emu_enc.py [path/to/OemDrv.exe]   (needs: pip install unicorn pefile)
Entry struct: dword [0]=type<<24|code; type 1 = mouse buttons, 8 = special functions, 2 = keyboard (extra bytes at +0x1c..).
"""
from unicorn import *
from unicorn.x86_const import *
EXE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('OEMDRV', 'OemDrv.exe')
pe = pefile.PE(EXE); base=pe.OPTIONAL_HEADER.ImageBase
img = pe.get_memory_mapped_image()
def make():
    mu = Uc(UC_ARCH_X86, UC_MODE_32)
    size = (len(img)+0xfff)&~0xfff
    mu.mem_map(base, size+0x100000)          # image + bss slack (globals live above the file image)
    mu.mem_write(base, bytes(img))
    mu.mem_map(0x10000000, 0x100000)         # stack
    mu.mem_map(0x20000000, 0x10000)          # entry struct
    mu.mem_map(0x30000000, 0x1000)           # return sentinel
    return mu
def encode(mu, typ, code, extra=b""):
    ent = struct.pack('<I', (typ<<24)|code) + b"\0"*0x40
    mu.mem_write(0x20000000, ent + b"\0"*0x100)
    if extra: mu.mem_write(0x20000000+0x1c, extra)
    sp = 0x10000000+0x80000
    mu.mem_write(sp, struct.pack('<II', 0x30000000, 0x20000000))   # ret addr, arg
    mu.reg_write(UC_X86_REG_ESP, sp); mu.reg_write(UC_X86_REG_ECX, 0)
    try:
        mu.emu_start(0x416580, 0x30000000, timeout=2000000, count=5000)
    except UcError as e:
        return None
    return mu.reg_read(UC_X86_REG_EAX)
if __name__=="__main__":
    mu = make()
    # sanity: mouse buttons (type 1) vs known defaults
    for code in range(0x11,0x1a):
        v = encode(mu,1,code); print("type1 %#x ->"%code, None if v is None else f"{v:#010x}")
    print("--- type 8")
    for code in range(0xA2,0xD2):
        v = encode(mu,8,code)
        print(f"type8 {code:#04x} -> " + ("fail" if v is None else struct.pack('<I',v).hex(' ')))
