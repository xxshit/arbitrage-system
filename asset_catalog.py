"""Public product classification, not ticker-suffix guessing or trade authority.

Bootstrap checked against Binance exchangeInfo / Bybit instruments-info on
2026-10-19. Live metadata and persisted classifications supplement this guard.
An absent/delisted product must not turn its old alerts back into crypto.
"""
import re


def compact_symbol(value):
    return re.sub(r'[/_\-\s]', '', str(value or '')).upper()


def pairs(bases):
    return {base + 'USDT' for base in bases.split()}


US_EQUITY_BOOTSTRAP = pairs('''
AAOI AAPL ACN ADBE AGPU AKAM ALAB AMAT AMC AMD AMZN ANET APLD APP ARM ASML ASTS AVGO AXTI
BABA BBX BE BITO BMNR BNC BOT BRKB BSP BWET BX CAT CBRS CIEN COHR COIN COST CRCL CRDO CRML
CRM CRWD CRWV CSCO CVNA CYPH DDOG DELL DIS DJT DKNG DRAM EBAY EWJ EWT EWY EWZ FLEX FLNC FWDI
GDX GEV GLW GME GOOGL GPRO GS GTLB HD HIMS HOOD HPE HUT IBM INTC INTW IONQ IREN IWM JPM
KLAC KORU KO KSTR LITE LLY LRCX LYTE MARA MCD MDB META MP MRK MRNA MRVL MSFT MSTR MSTU MU
MUU MVLL NBIS NET NFLX NKE NOK NOW NVDA NVDL NVO OKLO ONDS ORCL PANW PATH PAYP PDD PENG
PLTR PYPL QCOM QNTX QQQ RAM RDDT RDW RIVN RKLB RUM SECZ SHAZ SHOP SKDD SKHY SKUU SMCI SMH
SNDK SNOW SNPS SNXX SOFI SONY SOXL SOXS SPCX SPY SQQQ STRC STXX TBT TEAM TEM TER TMF TQQQ
TSLA TSLL TSM TTWO TWST TXN TZA UBER UNH URNM USAR USDEX UVXY VKTX VRT VST V WDC WEN WMT
XBI XLE XOM ZCSH ZM ZS
''')
OTHER_EQUITY_BOOTSTRAP = pairs('''
HANMI HYUNDAI KODEX200 LGELECTRONICS NAVER SAMSUNGEM SAMSUNG SKHYNIX BYD CSOPSAMSUNG2L
CSOPSKHYNIX2L GIGADEV HK0625 HK0700 HK0992 HK1810 KUAISHOU MEITUAN MINIMAX POPMART TENCENT
ZHIPU ZHONGJI CXMT UNITREE
''')
COMMODITY_BOOTSTRAP = pairs('BZ CL COPPER NATGAS XAG XAU XPD XPT')
PREMARKET_BOOTSTRAP = pairs('ANTHROPIC MOONSHOT OPENAI OURA')
BYBIT_EQUITY_BOOTSTRAP = pairs('''
AAL AAOI AAPL ACN ADBE ADI AEHR ALAB AMAT AMC AMDSTOCK AMGN AMZN AMZU ANET APLD APPSTOCK
ARM ASML ASTS AVGO AXTI BABA BAC BBX BE BIIB BMNR BNC BOT BRKB BSP BTDR BX BYD CATSTOCK
CBRS CGNX CIEN CIFR CLSK COHR COIN CORZ COST CRCL CRDO CRM CRWD CRWV CSCO CVNA CVXSTOCK CXMT
CYPH DDOG DELL DJT DKNG EBAY FLEX FLNC FLY FORMSTOCK FUTU FWDI GE GEV GIGADEVICE GILD GLW
GMESTOCK GOOGL GPRO GRAL GS GTLB HANMI HD HIMS HOOD HORIZON HPE HUAHONG HUT HYUNDAI IBM INFQ
INTC INTU IONQ IREN ISRG JD JNJ JPM KLAC KO KUAISHOU LAOPU LENOVO LGELECTRONICS LITE LLY LRCX
LULU MARA MA MCD MDB MEITUAN MEITU MELI META MINIMAX MNST MO MP MRK MRNA MRVL MSFT MSTR MU
NAVER NBIS NET NFLX NKE NOKIA NOW NUSTOCK NVDA NVO OKLO OKTA ONDS ON ORCL OUST PANW PATH PBR
PDD PENGSTOCK PEP PLTR POET POPMART PURR PYPL QCOM QNTX RDDT RDW REGN RIOT RIVN RKLB RUM
SAMSUNGEM SAMSUNG SDGR SECZ SENSETIME SHAZ SHEIN SHOP SKHYNIX SKHY SMCI SMIC SNDK SNOW SNPS
SOFI SONY SPCX SPOT STXX TEAM TEM TENCENT TER TSEM TSLA TSM TTWO TWST TXN UBER UMC UNH UNITREE
USAR USDEX VIST VKTX VRT VRTXSTOCK VST V WDC WENSTOCK WMT XIAOMI XOM YOFC YPF ZHIPU ZHONGJI ZM ZS
ARKK BITO BMNU BWET CONL CSOPSAMSUNG2L CSOPSKHYNIX2L DRAM EWJ EWT EWY EWZ GDX IBIT INTW IWM
KODEX200 KORU KSTR LYTE MAGS MSFU MSTU MUU MVLL NCLD NVDL QQQ RAM SKDD SKUU SMH SNXX SOXL SOXS
SOXX SPCH SPXL SPY SQQQ SSPC TBT TLT TMF TQQQ TSLL TZA URNM UVXY XBI XLE XLF XLK XLP XLU XLV ZCSH
''')
BYBIT_COMMODITIES = pairs('BZ CL XAG XAU')
FOREX_BOOTSTRAP = pairs('USDBRL EURUSD GBPUSD USDJPY')
NON_CRYPTO_BOOTSTRAP = (US_EQUITY_BOOTSTRAP | OTHER_EQUITY_BOOTSTRAP | COMMODITY_BOOTSTRAP |
                        PREMARKET_BOOTSTRAP | BYBIT_EQUITY_BOOTSTRAP | FOREX_BOOTSTRAP)


def instrument_category(item, exchange):
    if exchange == 'Binance':
        kind = str(item.get('underlyingType', '')).upper()
        if kind == 'EQUITY':
            return 'us_equity'
        if kind.endswith('_EQUITY'):
            return 'other_equity'
        if kind in ('COMMODITY', 'FX', 'PREMARKET'):
            return {'COMMODITY': 'commodity', 'FX': 'forex', 'PREMARKET': 'premarket'}[kind]
        tags = item.get('underlyingSubType') or []
        if item.get('contractType') in ('TRADIFI_PERPETUAL', 'TRADFI_PERPETUAL') or any(str(tag).lower() == 'tradfi' for tag in tags):
            return 'tradfi_unknown'
    if exchange == 'Bybit':
        # Bybit stock/ETF does not identify the listing country. It is not by
        # itself evidence that a Hong Kong or Korean product is a US listing.
        return {'stock': 'equity', 'etf': 'equity', 'commodity': 'commodity',
                'forex': 'forex'}.get(str(item.get('symbolType', '')).lower())
    return None


def bootstrap_category(symbol, exchange=None):
    symbol = compact_symbol(symbol)
    if exchange in (None, 'Binance'):
        for category, values in (('us_equity', US_EQUITY_BOOTSTRAP), ('other_equity', OTHER_EQUITY_BOOTSTRAP),
                                 ('commodity', COMMODITY_BOOTSTRAP), ('premarket', PREMARKET_BOOTSTRAP),
                                 ('forex', {'USDBRLUSDT'})):
            if symbol in values:
                return category
    if exchange in (None, 'Bybit'):
        if symbol in BYBIT_EQUITY_BOOTSTRAP:
            return 'equity'
        if symbol in BYBIT_COMMODITIES:
            return 'commodity'
        if symbol in FOREX_BOOTSTRAP - {'USDBRLUSDT'}:
            return 'forex'
    return None
