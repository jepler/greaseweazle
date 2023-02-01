# greaseweazle/codec/ibm/mfm.py
#
# Written & released by Keir Fraser <keir.xen@gmail.com>
#
# This is free and unencumbered software released into the public domain.
# See the file COPYING for more details, or visit <http://unlicense.org>.

import copy, heapq, struct, functools
import itertools as it
from bitarray import bitarray
import crcmod.predefined

from greaseweazle.track import MasterTrack, RawTrack
from .ibm import IDAM, DAM, Sector, IAM, IBMTrack

default_revs = 2

iam_sync_bytes = b'\x52\x24' * 3
iam_sync = bitarray(endian='big')
iam_sync.frombytes(iam_sync_bytes)

sync_bytes = b'\x44\x89' * 3
sync = bitarray(endian='big')
sync.frombytes(sync_bytes)

crc16 = crcmod.predefined.Crc('crc-ccitt-false')

class IBM_MFM(IBMTrack):

    gap_presync = 12

    gapbyte = 0x4e

    def summary_string(self):
        nsec, nbad = len(self.sectors), self.nr_missing()
        s = "IBM MFM (%d/%d sectors)" % (nsec - nbad, nsec)
        return s


    def decode_raw(self, track, pll=None):
        flux = track.flux()
        flux.cue_at_index()
        raw = RawTrack(time_per_rev = self.time_per_rev,
                       clock = self.clock, data = flux, pll = pll)
        bits, _ = raw.get_all_data()

        areas = []
        idam = None

        ## 1. Calculate offsets within dump
        
        for offs in bits.itersearch(iam_sync):
            if len(bits) < offs+4*16:
                continue
            mark = decode(bits[offs+3*16:offs+4*16].tobytes())[0]
            if mark == IBM_MFM.IAM:
                areas.append(IAM(offs, offs+4*16))
                self.has_iam = True

        for offs in bits.itersearch(sync):

            if len(bits) < offs+4*16:
                continue
            mark = decode(bits[offs+3*16:offs+4*16].tobytes())[0]
            if mark == IBM_MFM.IDAM:
                s, e = offs, offs+10*16
                if len(bits) < e:
                    continue
                b = decode(bits[s:e].tobytes())
                c,h,r,n = struct.unpack(">4x4B2x", b)
                crc = crc16.new(b).crcValue
                if idam is not None:
                    areas.append(idam)
                idam = IDAM(s, e, crc, c=c, h=h, r=r, n=n)
            elif mark == IBM_MFM.DAM or mark == IBM_MFM.DDAM:
                if idam is None or idam.end - offs > 1000:
                    areas.append(DAM(offs, offs+4*16, 0xffff, mark=mark))
                else:
                    sz = 128 << idam.n
                    s, e = offs, offs+(4+sz+2)*16
                    if len(bits) < e:
                        continue
                    b = decode(bits[s:e].tobytes())
                    crc = crc16.new(b).crcValue
                    dam = DAM(s, e, crc, mark=mark, data=b[4:-2])
                    areas.append(Sector(idam, dam))
                idam = None
            else:
                pass #print("Unknown mark %02x" % mark)

        if idam is not None:
            areas.append(idam)

        # Convert to offsets within track
        areas.sort(key=lambda x:x.start)
        index = iter(raw.revolutions)
        p, n = 0, next(index)
        for a in areas:
            if a.start >= n:
                p = n
                try:
                    n += next(index)
                except StopIteration:
                    n = float('inf')
            a.delta(p)
        areas.sort(key=lambda x:x.start)

        # Add to the deduped lists
        for a in areas:
            if isinstance(a, IAM):
                list = self.iams
            elif isinstance(a, Sector):
                list = self.sectors
            else:
                continue
            for i, s in enumerate(list):
                if abs(s.start - a.start) < 1000:
                    if isinstance(a, Sector) and s.crc != 0 and a.crc == 0:
                        self.sectors[i] = a
                    a = None
                    break
            if a is not None:
                list.append(a)


    def raw_track(self):

        areas = heapq.merge(self.iams, self.sectors, key=lambda x:x.start)
        t = bytes()

        for a in areas:
            start = a.start//16 - self.gap_presync
            gap = max(start - len(t)//2, 0)
            t += encode(bytes([self.gapbyte] * gap))
            t += encode(bytes(self.gap_presync))
            if isinstance(a, IAM):
                t += iam_sync_bytes
                t += encode(bytes([self.IAM]))
            elif isinstance(a, Sector):
                t += sync_bytes
                idam = bytes([0xa1, 0xa1, 0xa1, self.IDAM,
                              a.idam.c, a.idam.h, a.idam.r, a.idam.n])
                idam += struct.pack('>H', crc16.new(idam).crcValue)
                t += encode(idam[3:])
                start = a.dam.start//16 - self.gap_presync
                gap = max(start - len(t)//2, 0)
                t += encode(bytes([self.gapbyte] * gap))
                t += encode(bytes(self.gap_presync))
                t += sync_bytes
                dam = bytes([0xa1, 0xa1, 0xa1, a.dam.mark]) + a.dam.data
                dam += struct.pack('>H', crc16.new(dam).crcValue)
                t += encode(dam[3:])

        # Add the pre-index gap.
        tlen = int((self.time_per_rev / self.clock) // 16)
        gap = max(tlen - len(t)//2, 0)
        t += encode(bytes([self.gapbyte] * gap))

        track = MasterTrack(
            bits = mfm_encode(t),
            time_per_rev = self.time_per_rev)
        track.verify = self
        track.verify_revs = default_revs
        return track


class IBM_MFM_Formatted(IBM_MFM):

    def __init__(self, cyl, head):
        super().__init__(cyl, head)
        self.raw_iams, self.raw_sectors = [], []
        self.img_bps = None

    def decode_raw(self, track, pll=None):
        iams, sectors = self.iams, self.sectors
        self.iams, self.sectors = self.raw_iams, self.raw_sectors
        super().decode_raw(track, pll)
        self.iams, self.sectors = iams, sectors
        mismatches = set()
        for r in self.raw_sectors:
            if r.idam.crc != 0:
                continue
            matched = False
            for s in self.sectors:
                if (s.idam.c == r.idam.c and
                    s.idam.h == r.idam.h and
                    s.idam.r == r.idam.r and
                    s.idam.n == r.idam.n):
                    s.idam.crc = 0
                    matched = True
                    if r.dam.crc == 0 and s.dam.crc != 0:
                        s.dam.crc = s.crc = 0
                        s.dam.data = r.dam.data
            if not matched:
                mismatches.add((r.idam.c, r.idam.h, r.idam.r, r.idam.n))
        for m in mismatches:
            print('T%d.%d: Ignoring unexpected sector C:%d H:%d R:%d N:%d'
                  % (self.cyl, self.head, *m))

    def set_img_track(self, tdat):
        pos = 0
        self.sectors.sort(key = lambda x: x.idam.r)
        if self.img_bps is not None:
            totsize = len(self.sectors) * self.img_bps
        else:
            totsize = functools.reduce(lambda x, y: x + (128<<y.idam.n),
                                       self.sectors, 0)
        if len(tdat) < totsize:
            tdat += bytes(totsize - len(tdat))
        for s in self.sectors:
            s.crc = s.idam.crc = s.dam.crc = 0
            size = 128 << s.idam.n
            s.dam.data = tdat[pos:pos+size]
            if self.img_bps is not None:
                pos += self.img_bps
            else:
                pos += size
        self.sectors.sort(key = lambda x: x.start)
        return totsize

    def get_img_track(self):
        tdat = bytearray()
        sectors = self.sectors.copy()
        sectors.sort(key = lambda x: x.idam.r)
        for s in sectors:
            tdat += s.dam.data
            if self.img_bps is not None:
                tdat += bytes(self.img_bps - len(s.dam.data))
        return tdat
        
    def verify_track(self, flux):
        readback_track = IBM_MFM_Formatted(self.cyl, self.head)
        readback_track.clock = self.clock
        readback_track.time_per_rev = self.time_per_rev
        for x in self.iams:
            readback_track.iams.append(copy.copy(x))
        for x in self.sectors:
            idam, dam = copy.copy(x.idam), copy.copy(x.dam)
            idam.crc, dam.crc = 0xffff, 0xffff
            readback_track.sectors.append(Sector(idam, dam))
        readback_track.decode_raw(flux)
        if readback_track.nr_missing() != 0:
            return False
        return self.sectors == readback_track.sectors

    GAP_4A = 80 # Post-Index
    GAP_1  = 50 # Post-IAM
    GAP_2  = 22 # Post-IDAM
    GAP_3  = [ 32, 54, 84, 116, 255, 255, 255, 255 ]

    @classmethod
    def from_config(cls, config, cyl, head):

        def sec_n(i):
            return config.sz[i] if i < len(config.sz) else config.sz[-1]

        t = cls(cyl, head)
        t.nsec = nsec = config.secs
        t.img_bps = config.img_bps

        if config.iam:
            gap_1 = t.GAP_1 if config.gap1 is None else config.gap1
        else:
            gap_1 = None
        gap_2 = t.GAP_2 if config.gap2 is None else config.gap2
        gap_3 = 0 if config.gap3 is None else config.gap3
        gap_4a = t.GAP_4A if config.gap4a is None else config.gap4a

        idx_sz = gap_4a
        if gap_1 is not None:
            idx_sz += t.gap_presync + 4 + gap_1
        idam_sz = t.gap_presync + 8 + 2 + gap_2
        dam_sz_pre = t.gap_presync + 4
        dam_sz_post = 2 + gap_3

        tracklen = idx_sz + (idam_sz + dam_sz_pre + dam_sz_post) * nsec
        for i in range(nsec):
            tracklen += 128 << sec_n(i)
        tracklen *= 16

        rate, rpm = config.rate, config.rpm
        if rate == 0:
            for i in range(1, 4): # DD=1, HD=2, ED=3
                maxlen = ((50000*300//rpm) << i) + 5000
                if tracklen < maxlen:
                    break
            rate = 125 << i # DD=250, HD=500, ED=1000

        if config.gap2 is None and rate >= 1000:
            # At ED rate the default GAP2 is 41 bytes.
            old_gap_2 = gap_2
            gap_2 = 41
            idam_sz += gap_2 - old_gap_2
            tracklen += 16 * nsec * (gap_2 - old_gap_2)
            
        tracklen_bc = rate * 400 * 300 // rpm

        if nsec != 0 and config.gap3 is None:
            space = max(0, tracklen_bc - tracklen)
            no = sec_n(0)
            gap_3 = min(space // (16*nsec), t.GAP_3[no])
            dam_sz_post += gap_3
            tracklen += 16 * nsec * gap_3

        tracklen_bc = max(tracklen_bc, tracklen)

        t.time_per_rev = 60 / rpm
        t.clock = t.time_per_rev / tracklen_bc

        # Create logical sector map in rotational order
        sec_map, pos = [-1] * nsec, 0
        if nsec != 0:
            pos = (cyl*config.cskew + head*config.hskew) % nsec
        for i in range(nsec):
            while sec_map[pos] != -1:
                pos = (pos + 1) % nsec
            sec_map[pos] = i
            pos = (pos + config.interleave) % nsec

        pos = gap_4a
        if gap_1 is not None:
            pos += t.gap_presync
            t.iams = [IAM(pos*16,(pos+4)*16)]
            pos += 4 + gap_1

        id0 = config.id
        h = head if config.h is None else config.h
        for i in range(nsec):
            sec = sec_map[i]
            pos += t.gap_presync
            idam = IDAM(pos*16, (pos+10)*16, 0xffff,
                        c = cyl, h = h, r = id0+sec, n = sec_n(sec))
            pos += 10 + gap_2 + t.gap_presync
            size = 128 << idam.n
            dam = DAM(pos*16, (pos+4+size+2)*16, 0xffff,
                      mark=t.DAM, data=b'-=[BAD SECTOR]=-'*(size//16))
            t.sectors.append(Sector(idam, dam))
            pos += 4 + size + 2 + gap_3

        return t


def mfm_encode(dat):
    y = 0
    out = bytearray()
    for x in dat:
        y = (y<<8) | x
        if (x & 0xaa) == 0:
            y |= ~((y>>1)|(y<<1)) & 0xaaaa
        y &= 255
        out.append(y)
    return bytes(out)

encode_list = []
for x in range(256):
    y = 0
    for i in range(8):
        y <<= 2
        y |= (x >> (7-i)) & 1
    encode_list.append(y)

def encode(dat):
    out = bytearray()
    for x in dat:
        out += struct.pack('>H', encode_list[x])
    return bytes(out)
    
decode_list = bytearray()
for x in range(0x5555+1):
    y = 0
    for i in range(16):
        if x&(1<<(i*2)):
            y |= 1<<i
    decode_list.append(y)

def decode(dat):
    out = bytearray()
    for x,y in zip(dat[::2], dat[1::2]):
        out.append(decode_list[((x<<8)|y)&0x5555])
    return bytes(out)


# Local variables:
# python-indent: 4
# End:
