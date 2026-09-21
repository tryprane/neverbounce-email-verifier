// Create a function that looks like a native getter
const nativeGetter = function get webdriver() {
    return false;
};

// Copy over native function properties
Object.defineProperties(nativeGetter, {
    name: { value: 'get webdriver', configurable: true },
    length: { value: 0, configurable: true },
    toString: {
        value: function() {
            return `function get webdriver() { [native code] }`;
        },
        configurable: true
    }
});

// Make it look native
Object.setPrototypeOf(nativeGetter, Function.prototype);

// Apply the modified descriptor
Object.defineProperty(Navigator.prototype, 'webdriver', {
    get: nativeGetter,
    set: undefined,
    enumerable: true,
    configurable: true
});

// To escape `HEADCHR_CHROME_OBJ` test in headless mode => https://github.com/antoinevastel/fp-collect/blob/master/src/fpCollect.js#L322
// Faking window.chrome fully

if (!window.chrome) {
    // First, save all existing properties
    const originalKeys = Object.getOwnPropertyNames(window);
    const tempObj = {};

    // Recreate all properties in original order
    for (const key of originalKeys) {
        const descriptor = Object.getOwnPropertyDescriptor(window, key);
        const value = window[key];
        // delete window[key];
        Object.defineProperty(tempObj, key, descriptor);
    }

    // Use the exact property descriptor found in headful Chrome
    // fetch it via `Object.getOwnPropertyDescriptor(window, 'chrome')`
    const mockChrome = {
        loadTimes: {},
        csi: {},
        app: {
            isInstalled: false
        },
        // Add other Chrome-specific properties
    };

    Object.defineProperty(tempObj, 'chrome', {
        writable: true,
        enumerable: true,
        configurable: false,
        value: mockChrome
    });
    for (const key of Object.getOwnPropertyNames(tempObj)) {
        try {
            Object.defineProperty(window, key,
                Object.getOwnPropertyDescriptor(tempObj, key));
        } catch (e) {}
    };
    // todo: solve this
    // Using line below bypasses the hasHighChromeIndex test in creepjs ==> https://github.com/abrahamjuliot/creepjs/blob/master/src/headless/index.ts#L121
    // Chrome object have to be in the end of the window properties
    // Object.assign(window, tempObj);
    // But makes window.chrome unreadable on 'https://bot.sannysoft.com/'
}

// That means we're running headful and don't need to mock anything
if ('app' in window.chrome) {
    return; // Nothing to do here
}
const makeError = {
    ErrorInInvocation: fn => {
        const err = new TypeError(`Error in invocation of app.${fn}()`);
        return utils.stripErrorWithAnchor(
            err,
            `at ${fn} (eval at <anonymous>`,
        );
    },
};
// check with: `JSON.stringify(window.chrome['app'])`
const STATIC_DATA = JSON.parse(
    `
{
  "isInstalled": false,
  "InstallState": {
    "DISABLED": "disabled",
    "INSTALLED": "installed",
    "NOT_INSTALLED": "not_installed"
  },
  "RunningState": {
    "CANNOT_RUN": "cannot_run",
    "READY_TO_RUN": "ready_to_run",
    "RUNNING": "running"
  }
}
    `.trim(),
    );
window.chrome.app = {
    ...STATIC_DATA,

    get isInstalled() {
        return false;
    },

    getDetails: function getDetails() {
        if (arguments.length) {
            throw makeError.ErrorInInvocation(`getDetails`);
        }
        return null;
    },
    getIsInstalled: function getDetails() {
        if (arguments.length) {
            throw makeError.ErrorInInvocation(`getIsInstalled`);
        }
        return false;
    },
    runningState: function getDetails() {
        if (arguments.length) {
            throw makeError.ErrorInInvocation(`runningState`);
        }
        return 'cannot_run';
    },
};
// Check that the Navigation Timing API v1 is available, we need that
if (!window.performance || !window.performance.timing) {
    return;
}
const {timing} = window.performance;
window.chrome.csi = function () {
    return {
        onloadT: timing.domContentLoadedEventEnd,
        startE: timing.navigationStart,
        pageT: Date.now() - timing.navigationStart,
        tran: 15, // Transition type or something
    };
};
if (!window.PerformancePaintTiming){
    return;
}
const {performance} = window;
// Some stuff is not available on about:blank as it requires a navigation to occur,
// let's harden the code to not fail then:
const ntEntryFallback = {
    nextHopProtocol: 'h2',
    type: 'other',
};

// The API exposes some funky info regarding the connection
const protocolInfo = {
    get connectionInfo() {
        const ntEntry =
            performance.getEntriesByType('navigation')[0] || ntEntryFallback;
        return ntEntry.nextHopProtocol;
    },
    get npnNegotiatedProtocol() {
        // NPN is deprecated in favor of ALPN, but this implementation returns the
        // HTTP/2 or HTTP2+QUIC/39 requests negotiated via ALPN.
        const ntEntry =
            performance.getEntriesByType('navigation')[0] || ntEntryFallback;
        return ['h2', 'hq'].includes(ntEntry.nextHopProtocol)
            ? ntEntry.nextHopProtocol
            : 'unknown';
    },
    get navigationType() {
        const ntEntry =
            performance.getEntriesByType('navigation')[0] || ntEntryFallback;
        return ntEntry.type;
    },
    get wasAlternateProtocolAvailable() {
        // The Alternate-Protocol header is deprecated in favor of Alt-Svc
        // (https://www.mnot.net/blog/2016/03/09/alt-svc), so technically this
        // should always return false.
        return false;
    },
    get wasFetchedViaSpdy() {
        // SPDY is deprecated in favor of HTTP/2, but this implementation returns
        // true for HTTP/2 or HTTP2+QUIC/39 as well.
        const ntEntry =
            performance.getEntriesByType('navigation')[0] || ntEntryFallback;
        return ['h2', 'hq'].includes(ntEntry.nextHopProtocol);
    },
    get wasNpnNegotiated() {
        // NPN is deprecated in favor of ALPN, but this implementation returns true
        // for HTTP/2 or HTTP2+QUIC/39 requests negotiated via ALPN.
        const ntEntry =
            performance.getEntriesByType('navigation')[0] || ntEntryFallback;
        return ['h2', 'hq'].includes(ntEntry.nextHopProtocol);
    },
};

// Truncate number to specific number of decimals, most of the `loadTimes` stuff has 3
function toFixed(num, fixed) {
    var re = new RegExp('^-?\\d+(?:.\\d{0,' + (fixed || -1) + '})?');
    return num.toString().match(re)[0];
}

const timingInfo = {
    get firstPaintAfterLoadTime() {
        // This was never actually implemented and always returns 0.
        return 0;
    },
    get requestTime() {
        return timing.navigationStart / 1000;
    },
    get startLoadTime() {
        return timing.navigationStart / 1000;
    },
    get commitLoadTime() {
        return timing.responseStart / 1000;
    },
    get finishDocumentLoadTime() {
        return timing.domContentLoadedEventEnd / 1000;
    },
    get finishLoadTime() {
        return timing.loadEventEnd / 1000;
    },
    get firstPaintTime() {
        const fpEntry = performance.getEntriesByType('paint')[0] || {
            startTime: timing.loadEventEnd / 1000, // Fallback if no navigation occured (`about:blank`)
        };
        return toFixed(
            (fpEntry.startTime + performance.timeOrigin) / 1000,
            3,
        );
    },
};

window.chrome.loadTimes = function () {
    return {
        ...protocolInfo,
        ...timingInfo,
    };
};

if(navigator.plugins.length == 0){
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const PDFViewerPlugin = Object.create(Plugin.prototype, {
                description: { value: 'Portable Document Format', enumerable: false },
                filename: { value: 'internal-pdf-viewer', enumerable: false },
                name: { value: 'PDF Viewer', enumerable: false },
            });
            const ChromePDFViewer = Object.create(Plugin.prototype, {
                description: { value: 'Portable Document Format', enumerable: false },
                filename: { value: 'internal-pdf-viewer', enumerable: false },
                name: { value: 'Chrome PDF Viewer', enumerable: false },
            });
            const ChromiumPDFViewer = Object.create(Plugin.prototype, {
                description: { value: 'Portable Document Format', enumerable: false },
                filename: { value: 'internal-pdf-viewer', enumerable: false },
                name: { value: 'Chromium PDF Viewer', enumerable: false },
            });
            const EdgePDFViewer = Object.create(Plugin.prototype, {
                description: { value: 'Portable Document Format', enumerable: false },
                filename: { value: 'internal-pdf-viewer', enumerable: false },
                name: { value: 'Microsoft Edge PDF Viewer', enumerable: false },
            });
            const WebKitPDFPlugin = Object.create(Plugin.prototype, {
                description: { value: 'Portable Document Format', enumerable: false },
                filename: { value: 'internal-pdf-viewer', enumerable: false },
                name: { value: 'WebKit built-in PDF', enumerable: false },
            });

            return Object.create(PluginArray.prototype, {
                length: { value: 5 },
                0: { value: PDFViewerPlugin },
                1: { value: ChromePDFViewer },
                2: { value: ChromiumPDFViewer },
                3: { value: EdgePDFViewer },
                4: { value: WebKitPDFPlugin },
            });
        },
    });
}

// Bypasses `notificationIsDenied` test in creepsjs's 'Like Headless' sections
const isSecure = document.location.protocol.startsWith('https')
if (isSecure){
    Object.defineProperty(Notification, 'permission', {get: () => 'default'})
}

const windowScreenProps = {
    // Dimensions
    innerHeight: 0,
    innerWidth: 0,
    outerHeight: 754,
    outerWidth: 1313,

    // Position
    screenX: 19,
    pageXOffset: 0,
    pageYOffset: 0,

    // Display
    devicePixelRatio: 2
};

try {
    for (const [prop, value] of Object.entries(windowScreenProps)) {
        if (value > 0) {
            // The 0 values are introduced by collecting in the hidden iframe.
            // They are document sizes anyway so no need to test them or inject them.
            window[prop] = value;
        }
    }
} catch (e) {
    console.warn(e);
};

// Remove playwright fingerprint => https://github.com/microsoft/playwright/commit/c9e673c6dca746384338ab6bb0cf63c7e7caa9b2#diff-087773eea292da9db5a3f27de8f1a2940cdb895383ad750c3cd8e01772a35b40R915
delete window.__pwInitScripts;
delete window.__playwright__binding__;