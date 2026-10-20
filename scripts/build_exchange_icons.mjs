// Package the existing tiny PNGs once, without fetching or redrawing brand assets.
// Run with --check to validate the bundle, or without arguments to print it.
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';

export function buildExchangeIcons(){
  const icons=Object.fromEntries(['Binance','Gate','Bitget','Bybit','OKX'].map(exchange=>{
    const bytes=readFileSync(new URL(`../static/exchanges/${exchange.toLowerCase()}.png`,import.meta.url));
    return [exchange,`data:image/png;base64,${bytes.toString('base64')}`];
  }));
  return '// Generated from static/exchanges/*.png by scripts/build_exchange_icons.mjs; do not edit by hand.\n'+
    'const embeddedExchangeIcons=Object.freeze('+JSON.stringify(icons,null,2)+');\n';
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const output=buildExchangeIcons();
  if(process.argv.includes('--check')){
    if(readFileSync(new URL('../static/exchange-icons.js',import.meta.url),'utf8')!==output){
      throw new Error('Exchange icon bundle differs from the original local PNGs');
    }
    console.log('Exchange icon bundle matches all five original PNGs.');
  }else process.stdout.write(output);
}
