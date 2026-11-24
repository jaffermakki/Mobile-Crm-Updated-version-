Third-party code served from this site (nothing is loaded from another website)

zxing-library-0.23.0.min.js
  What:     ZXing ("zebra crossing") barcode reader for JavaScript, the UMD build of @zxing/library 0.23.0.
  Used by:  static/scan.js, only on phones/browsers that have no built-in barcode reader (iPhone Safari, desktop
            browsers). Chrome on Android uses its own reader and never downloads this file.
  Licence:  Apache License 2.0 (see zxing-library-LICENSE.txt). Used unmodified.
  SHA-256:  3ede94153fb0c5b67a12d7adff6decd827c2b22714fdc6faecf27a8f20937ea6
  To update: replace the file, change the name in static/scan.js (ZX_URL) and in tests/test_v50_scan.py, run the tests.

zxing-wasm/  (v54.1)
  What:     zxing-wasm 3.1.7 (reader build): the ZXing-C++ barcode reader compiled to WebAssembly, with its small
            JavaScript wrapper. Much better than the JavaScript ZXing above at small barcodes (phone IMEI labels), dark
            surroundings and slightly blurry pictures, and about 4 ms per frame.
  Files:    zxing-wasm-reader-3.1.7.iife.js (the wrapper, SHA-256 1c61f43ac68da5c242f58c0d05763c295cd8a605d46d14bc31821eee7160e778)
            zxing_reader-3.1.7.wasm         (the reader, SHA-256 33c8c50be17d0bb05454b4fd3ffb5b5ab9a8e8d1c0ebeb21a1adf4a89c1a8b59)
  Used by:  static/scan.js. Loaded only when the scanner is opened. If a browser cannot run WebAssembly (or its security
            policy forbids it) scan.js falls back to the JavaScript ZXing above.
  Licence:  MIT for the wrapper (zxing-wasm-LICENSE.txt); the ZXing-C++ code inside it is Apache License 2.0.
  Security: the site's content-security-policy gains 'wasm-unsafe-eval' (needed for any WebAssembly). It does NOT allow
            'unsafe-eval'.
