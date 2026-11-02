'use strict';
const hid=require('node-hid');require('usb');const usb=require('usb/index.js');
if(typeof hid.devices!=='function'||typeof usb.nativeGetDevices!=='function')throw Error('native module API missing');
console.log('PASS: node-hid and usb native modules loaded; no device opened');
