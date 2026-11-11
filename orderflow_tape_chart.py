"""Exact data chart, rendered with matplotlib (no generated imagery)."""
from __future__ import annotations
from io import BytesIO
import textwrap

from orderflow_tape import clock, rp


def render_dashboard(m: dict, payloads: dict) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    bg, panel, fg, muted = "#080f1c", "#101c2d", "#e5edf8", "#9cacbf"
    green, red, cyan = "#19d3a2", "#ff577d", "#35c9ee"
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10,
                         "text.color": fg, "axes.labelcolor": muted,
                         "xtick.color": muted, "ytick.color": muted,
                         "axes.edgecolor": "#26364b", "axes.titlecolor": fg}):
        fig = plt.figure(figsize=(16, 11), facecolor=bg)
        gs = fig.add_gridspec(3, 3, height_ratios=[1.7, .8, 1.1], left=.055,
                              right=.965, top=.80, bottom=.135, wspace=.27, hspace=.55)
        bubble = fig.add_subplot(gs[0, :2])
        footprint = fig.add_subplot(gs[0, 2])
        cvd = fig.add_subplot(gs[1, :2])
        ratio = fig.add_subplot(gs[1, 2])
        size = fig.add_subplot(gs[2, 0])
        reading = fig.add_subplot(gs[2, 1])
        large = fig.add_subplot(gs[2, 2])
        for ax in (bubble, footprint, cvd, ratio, size, reading, large):
            ax.set_facecolor(panel)
        fig.text(.055,.95,f"{m['ticker']}  |  TAPE / FOOTPRINT",fontsize=24,weight="bold")
        fig.text(.055,.918,f"{m['label']}   •   {m['basis']}   •   snapshot {m['fetched'][:19].replace('T',' ')} WIB",fontsize=11,color=muted)
        fig.text(.055,.882,f"HAKA {rp(m['buy'])}    HAKI {rp(m['sell'])}    NET {rp(m['net'], True)}",fontsize=15,color=green if m['net']>=0 else red)
        fig.text(.055,.852,f"Sesi referensi: {m['session'] or 'N/A'} | Tanggal tape {'terverifikasi' if m['date_verified'] else 'BELUM terverifikasi'} | {len(m['trades'])} cetakan, maks. 3 halaman",color=muted)
        bubble.set_title("Bubble tape • cetakan tersedia, ukuran = nilai", loc="left",pad=12)
        trades=m["trades"]
        if trades:
            cap=max(r['value'] for r in trades)
            for action,col,marker in (("BUY",green,"o"),("SELL",red,"o")):
                subset=[r for r in trades if r['action']==action]
                bubble.scatter([r['seconds']/3600 for r in subset],[r['price'] for r in subset],
                               s=[20+500*r['value']/cap for r in subset],
                               facecolors=col if action=='BUY' else 'none',edgecolors=col,
                               alpha=.65,label='HAKA' if action=='BUY' else 'HAKI',marker=marker)
            top=max(trades,key=lambda r:r['value'])
            bubble.annotate(f"{rp(top['value'])} • {clock(top['seconds'])}",
                            (top['seconds']/3600,top['price']),xytext=(-8,16),ha='right',textcoords='offset points',fontsize=9,color=cyan)
            bubble.legend(loc='upper left',facecolor=panel,labelcolor=fg)
            bubble.xaxis.set_major_formatter(FuncFormatter(lambda v,_:clock(int(v*3600))[:5]))
        else:
            bubble.text(.5,.5,"Running trades tidak tersedia",ha='center',transform=bubble.transAxes,color=muted)
        for value,col,name in ((m['vwap_buy'],green,'VWAP HAKA'),(m['vwap_sell'],red,'VWAP HAKI'),(m['poc'],'#d7ba67','POC nilai')):
            if value is not None:
                bubble.axhline(value,color=col,ls='--',lw=.8,alpha=.7)
        bubble.grid(alpha=.08)
        footprint.set_title("Footprint per harga • nilai",loc='left',pad=12)
        selected=sorted(sorted(m['levels'],key=lambda r:r['buy']+r['sell'],reverse=True)[:12],key=lambda r:r['price'])
        y=list(range(len(selected)))
        footprint.barh(y,[-r['sell']/1e6 for r in selected],color=red,label='HAKI')
        footprint.barh(y,[r['buy']/1e6 for r in selected],color=green,label='HAKA')
        footprint.set_yticks(y,[f"{r['price']:g}" for r in selected])
        footprint.axvline(0,color=muted,lw=.7)
        footprint.set_xlabel("Rp juta • maks. 12 level terbesar")
        footprint.xaxis.set_major_formatter(FuncFormatter(lambda v,_:f"{abs(v):,.0f}"))
        cvd.set_title("CVD tape PARSIAL • nilai kumulatif dari awal sampel",loc='left',pad=12)
        if trades:
            cvd.plot([r['seconds']/3600 for r in trades],[v/1e6 for v in m['cvd']],color=green,lw=1.7)
            cvd.fill_between([r['seconds']/3600 for r in trades],[v/1e6 for v in m['cvd']],alpha=.1,color=green)
            cvd.xaxis.set_major_formatter(FuncFormatter(lambda v,_:clock(int(v*3600))[:5]))
        cvd.axhline(0,color=muted,lw=.5,ls=':')
        cvd.set_ylabel('Rp juta')
        ratio.set_title("HAKA vs HAKI • snapshot",loc='left',pad=12)
        ratio.barh([0],[m['ratio']*100],color=green,height=.4)
        ratio.barh([0],[(1-m['ratio'])*100],left=m['ratio']*100,color=red,height=.4)
        ratio.set_xlim(0,100);ratio.set_ylim(-.6,.6);ratio.set_yticks([])
        ratio.set_xlabel(f"HAKA {m['ratio']*100:.1f}%  /  HAKI {(1-m['ratio'])*100:.1f}%")
        size.set_title("Ukuran CETAKAN • sampel tape",loc='left',pad=12)
        labels=['<10 jt','10–50 jt','50–100 jt','100–500 jt','500 jt–1 M','1–5 M','≥5 M']
        size.barh(labels,m['buckets'],color=cyan,alpha=.8)
        size.invert_yaxis();size.xaxis.set_major_formatter(FuncFormatter(lambda v,_:f"{v/1e6:,.0f}"))
        size.set_xlabel('Total nilai per kelompok (Rp juta)')
        reading.set_title("Pembacaan arus • aturan transparan",loc='left',pad=12)
        reading.axis('off')
        stats=[f"Harga {m['last'] if m['last'] is not None else 'N/A'} / Open {m['open'] if m['open'] is not None else 'N/A'}", f"Menit net beli {m['minutes_buy']}/{m['minutes_count']} menit aktif",
               f"VWAP HAKA {m['vwap_buy']:,.2f}" if m['vwap_buy'] else 'VWAP HAKA N/A',
               f"VWAP HAKI {m['vwap_sell']:,.2f}" if m['vwap_sell'] else 'VWAP HAKI N/A',
               f"POC nilai {m['poc']:g}",textwrap.fill(m['reading'],39),
               'DNA / iceberg / identitas bandar: N/A']
        reading.text(0,.98,'\n'.join(stats),transform=reading.transAxes,va='top',linespacing=1.65,fontsize=9)
        large.set_title("Cetakan besar ≥Rp20 jt • terbaru",loc='left',pad=12)
        large.axis('off')
        table_rows=[[clock(r['seconds']), 'HAKA' if r['action']=='BUY' else 'HAKI',f"{r['price']:g}",rp(r['value'])] for r in m['big'][:6]]
        if table_rows:
            table=large.table(cellText=table_rows,colLabels=['Waktu','Sisi','Harga','Nilai'],loc='upper left',cellLoc='left',colWidths=[.25,.18,.16,.39])
            table.auto_set_font_size(False);table.set_fontsize(8);table.scale(1,1.5)
            for (r,c),cell in table.get_celld().items():
                cell.set_facecolor(panel);cell.set_edgecolor('#26364b');cell.get_text().set_color(fg)
                if r and c==1:cell.get_text().set_color(green if table_rows[r-1][1]=='HAKA' else red)
        else:
            large.text(0,.85,'Tidak ada dalam sampel.',color=muted)
        fig.text(.055,.056,"CAKUPAN: tape terbatas; CVD mulai nol. Tradebook/tape terpisah. Tanggal tape dan pemisahan nego/crossing belum tentu terverifikasi.",fontsize=9,color=muted)
        fig.text(.055,.032,"Ukuran cetakan bukan ukuran order asli. Baca ringkasan Telegram untuk peringatan sumber, tanggal, dan cakupan. Tidak ada scheduler tambahan.",fontsize=9,color=muted)
        output=BytesIO()
        try:
            fig.savefig(output,format='png',dpi=135,facecolor=bg)
            return output.getvalue()
        finally:
            plt.close(fig)
