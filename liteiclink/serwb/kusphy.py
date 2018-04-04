from migen import *
from migen.genlib.misc import BitSlip

from litex.soc.interconnect import stream
from litex.soc.cores.code_8b10b import Encoder, Decoder


class KUSSerdes(Module):
    tx_ready_latency = 2 # Encoder
    rx_valid_latency = 1 + 1 + 2  # Decoder + Sync + Bitslip
    def __init__(self, pads, mode="master"):
        if mode == "slave":
            self.refclk = Signal()

        self.tx_ready = Signal()
        self.tx_k = Signal(4)
        self.tx_d = Signal(32)
        self.rx_valid = Signal()
        self.rx_k = Signal(4)
        self.rx_d = Signal(32)

        self.tx_idle = Signal()
        self.tx_comma = Signal()
        self.rx_idle = Signal()
        self.rx_comma = Signal()

        self.rx_bitslip_value = Signal(6)
        self.rx_delay_rst = Signal()
        self.rx_delay_inc = Signal()
        self.rx_delay_ce = Signal()
        self.rx_delay_en_vtc = Signal()

        # # #

        self.submodules.encoder = CEInserter()(Encoder(4, True))
        self.decoders = [Decoder(True) for _ in range(4)]
        self.submodules += self.decoders

        # clocking:

        # In master mode:
        # - linerate/10 refclk generated on clk_pads
        # In Slave mode:
        # - linerate/10 refclk provided by clk_pads

        # tx clock (linerate/10)
        if mode == "master":
            self.submodules.tx_clk_converter = stream.Converter(40, 8)
            self.comb += [
                self.tx_clk_converter.sink.valid.eq(1),
                self.tx_clk_converter.sink.data.eq((0b1111100000 << 30) |
                                                   (0b1111100000 << 20) |
                                                   (0b1111100000 << 10) |
                                                   (0b1111100000 <<  0)),
                self.tx_clk_converter.source.ready.eq(1)
            ]
            clk_o = Signal()
            self.specials += [
                Instance("OSERDESE3",
                    p_DATA_WIDTH=8, p_INIT=0,
                    p_IS_CLK_INVERTED=0, p_IS_CLKDIV_INVERTED=0, p_IS_RST_INVERTED=0,

                    o_OQ=clk_o,
                    i_RST=ResetSignal("sys"),
                    i_CLK=ClockSignal("sys4x"), i_CLKDIV=ClockSignal("sys"),
                    i_D=self.tx_clk_converter.source.data
                ),
                Instance("OBUFDS",
                    i_I=clk_o,
                    o_O=pads.clk_p,
                    o_OB=pads.clk_n
                )
            ]

        # tx datapath
        # tx_data -> encoders -> converter -> serdes
        tx_ready_sr = Signal(self.tx_ready_latency)
        self.submodules.tx_converter = stream.Converter(40, 8)
        self.comb += self.tx_converter.source.ready.eq(1)
        self.comb += [
            If(self.tx_comma,
                self.encoder.k[0].eq(1),
                self.encoder.d[0].eq(0xbc)
            ).Else(
                self.encoder.k[0].eq(self.tx_k[0]),
                self.encoder.k[1].eq(self.tx_k[1]),
                self.encoder.k[2].eq(self.tx_k[2]),
                self.encoder.k[3].eq(self.tx_k[3]),
                self.encoder.d[0].eq(self.tx_d[0:8]),
                self.encoder.d[1].eq(self.tx_d[8:16]),
                self.encoder.d[2].eq(self.tx_d[16:24]),
                self.encoder.d[3].eq(self.tx_d[24:32])
            )
        ]
        self.comb += [
            self.encoder.ce.eq(self.tx_converter.sink.ready),
            self.tx_converter.sink.valid.eq(1),
             If(self.tx_idle,
                self.tx_converter.sink.data.eq(0)
            ).Else(
                self.tx_converter.sink.data.eq(Cat(*[self.encoder.output[i] for i in range(4)]))
            )
        ]
        self.sync += tx_ready_sr.eq(Cat(self.tx_converter.sink.ready, tx_ready_sr))
        self.comb += self.tx_ready.eq(tx_ready_sr[-1])

        serdes_o = Signal()
        self.specials += [
            Instance("OSERDESE3",
                p_DATA_WIDTH=8, p_INIT=0,
                p_IS_CLK_INVERTED=0, p_IS_CLKDIV_INVERTED=0, p_IS_RST_INVERTED=0,

                o_OQ=serdes_o,
                i_RST=ResetSignal("sys"),
                i_CLK=ClockSignal("sys4x"), i_CLKDIV=ClockSignal("sys"),
                i_D=self.tx_converter.source.data
            ),
            Instance("OBUFDS",
                i_I=serdes_o,
                o_O=pads.tx_p,
                o_OB=pads.tx_n
            )
        ]

        # rx clock
        use_bufr = True
        if mode == "slave":
            clk_i = Signal()
            clk_i_bufg = Signal()
            self.specials += [
                Instance("IBUFDS",
                    i_I=pads.clk_p,
                    i_IB=pads.clk_n,
                    o_O=clk_i
                )
            ]
            if use_bufr:
                clk_i_bufr = Signal()
                self.specials += [
                    Instance("BUFR", i_I=clk_i, o_O=clk_i_bufr),
                    Instance("BUFG", i_I=clk_i_bufr, o_O=clk_i_bufg)
                ]
            else:
                self.specials += Instance("BUFG", i_I=clk_i, o_O=clk_i_bufg)
            self.comb += self.refclk.eq(clk_i_bufg)

        # rx datapath
        # serdes -> converter -> bitslip -> decoders -> rx_data
        rx_valid_sr = Signal(self.rx_valid_latency)
        self.submodules.rx_converter = stream.Converter(8, 40)
        self.comb += self.rx_converter.source.ready.eq(1)
        self.submodules.rx_bitslip = BitSlip(40)

        serdes_i_nodelay = Signal()
        self.specials += [
            Instance("IBUFDS_DIFF_OUT",
                i_I=pads.rx_p,
                i_IB=pads.rx_n,
                o_O=serdes_i_nodelay
            )
        ]

        serdes_i_delayed = Signal()
        serdes_q = Signal(8)
        self.specials += [
            Instance("IDELAYE3",
                p_CASCADE="NONE", p_UPDATE_MODE="ASYNC", p_REFCLK_FREQUENCY=200.0,
                p_IS_CLK_INVERTED=0, p_IS_RST_INVERTED=0,
                p_DELAY_FORMAT="COUNT", p_DELAY_SRC="IDATAIN",
                p_DELAY_TYPE="VARIABLE", p_DELAY_VALUE=0,

                i_CLK=ClockSignal("sys"),
                i_RST=self.rx_delay_rst, i_LOAD=0,
                i_INC=self.rx_delay_inc, i_EN_VTC=self.rx_delay_en_vtc,
                i_CE=self.rx_delay_ce,

                i_IDATAIN=serdes_i_nodelay, o_DATAOUT=serdes_i_delayed
            ),
            Instance("ISERDESE3",
                p_IS_CLK_INVERTED=0,
                p_IS_CLK_B_INVERTED=1,
                p_DATA_WIDTH=8,

                i_D=serdes_i_delayed,
                i_RST=ResetSignal("sys"),
                i_FIFO_RD_CLK=0, i_FIFO_RD_EN=0,
                i_CLK=ClockSignal("sys4x"),
                i_CLK_B=ClockSignal("sys4x"), # locally inverted
                i_CLKDIV=ClockSignal("sys"),
                o_Q=serdes_q
            )
        ]
        self.sync += [
            rx_valid_sr.eq(Cat(self.rx_converter.source.valid, rx_valid_sr)),
            If(self.rx_converter.source.valid,
                self.rx_bitslip.i.eq(self.rx_converter.source.data)
            )
        ]
        self.comb += [
            self.rx_converter.sink.valid.eq(1),
            self.rx_converter.sink.data.eq(serdes_q),
            self.rx_bitslip.value.eq(self.rx_bitslip_value),
            self.decoders[0].input.eq(self.rx_bitslip.o[0:10]),
            self.decoders[1].input.eq(self.rx_bitslip.o[10:20]),
            self.decoders[2].input.eq(self.rx_bitslip.o[20:30]),
            self.decoders[3].input.eq(self.rx_bitslip.o[30:40]),
            self.rx_valid.eq(rx_valid_sr[-1]),
            self.rx_k.eq(Cat(*[self.decoders[i].k for i in range(4)])),
            self.rx_d.eq(Cat(*[self.decoders[i].d for i in range(4)])),
            self.rx_idle.eq(self.rx_bitslip.o == 0),
            self.rx_comma.eq(((self.decoders[0].d == 0xbc) & (self.decoders[0].k == 1)) &
                             ((self.decoders[1].d == 0x00) & (self.decoders[1].k == 0)) &
                             ((self.decoders[2].d == 0x00) & (self.decoders[2].k == 0)) &
                             ((self.decoders[3].d == 0x00) & (self.decoders[3].k == 0)))

        ]
