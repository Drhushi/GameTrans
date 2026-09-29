//=============================================================================
// GametransRuntime.js
//=============================================================================

/*:
 * @plugindesc gametrans 运行时翻译：引擎载入数据之后改写内存里的文本，因此
 * 游戏数据文件零改动、一份产物可带多种语言、设置菜单里可切换。
 * @author gametrans
 *
 * @param Default Language
 * @desc 默认语言代码，对应 translations/<代码>.json
 * @default zh
 *
 * @param Languages
 * @desc 这份产物带了哪些语言（逗号分隔）。只有一个时不会往设置菜单加语言行。
 * @default zh
 *
 * @param Translation Path
 * @desc 译文表目录（相对游戏根目录）
 * @default translations/
 *
 * @help
 * ============================================================================
 * 它怎么工作
 * ============================================================================
 *
 * 引擎把全部文本放在内存里的普通对象上（$dataSystem / $dataItems / $dataMap …），
 * 而那些读文本的窗口都是在**画的时候**才去读它们。所以只要在引擎载入数据之后把
 * 字符串换掉一次，所有消费方（引擎自己的窗口、其它插件、事件指令）就都看到译文 ——
 * 不需要逐个窗口去拦。
 *
 * 译文表按**位置**定键，键就是引擎数据结构里的路径：
 *
 *   {
 *     "Actors.json#1.name": "简",
 *     "Items.json#1.description": "一支医用笔。<br>大概吧。",
 *     "Map001.json#displayName": "一号房间",
 *     "Map001.json#events[1].pages[0].list[9].parameters[0]": "你好。",
 *     "Map001.json#events[1].pages[0].list[2].parameters[0][1]": "否"
 *   }
 *
 * 按位置定键有两个好处：同一句原文在不同地方可以有不同译法；游戏更新后只要位置
 * 没变，译文照旧对得上。
 *
 * 切语言时靠**原文快照**还原，再套上新语言的表 —— 所以来回切不会层层覆盖。
 *
 * @help
 * ============================================================================
 * 与"设置菜单"的接线
 * ============================================================================
 *
 * 本插件往设置菜单加一行、把它存进 config.rpgsave，并在切换后刷新窗口。
 * 注意：非语言行的 OK 键**必须交回引擎自己的 Window_Options.processOk**——
 * 音量与开关的值是那里面改的，绕过去就会出现"设置菜单调不动"。
 *
 * 事件指令：GametransLanguage zh
 * 脚本调用：$gametrans.setLanguage('zh');
 * ============================================================================
 */

(function () {
    'use strict';

    var PLUGIN_NAME = 'GametransRuntime';
    var LANGUAGE_COMMAND = 'Language / 语言';
    var LANGUAGE_LABELS = {
        zh: '中文', 'zh_cn': '中文', 'zh_tw': '繁體中文',
        en: 'English', ja: '日本語', ko: '한국어',
        fr: 'Français', de: 'Deutsch', es: 'Español', pt: 'Português', ru: 'Русский'
    };

    var parameters = PluginManager.parameters(PLUGIN_NAME);
    var DEFAULT_LANGUAGE = parameters['Default Language'] || 'zh';
    var TABLE_PATH = parameters['Translation Path'] || 'translations/';
    var CONFIGURED = String(parameters['Languages'] || DEFAULT_LANGUAGE)
        .split(',').map(function (s) { return s.trim(); }).filter(function (s) { return s; });

    // ------------------------------------------------------------------ 路径

    // "Map001.json#events[1].pages[0].list[9].parameters[0]" →
    //   { file: "Map001.json", keys: ["events","1","pages","0","list","9","parameters","0"] }
    // 数组与对象一视同仁：JS 里 arr["3"] 就是 arr[3]。
    function parseSpec(spec) {
        var hash = spec.indexOf('#');
        if (hash < 0) return { file: spec, keys: [] };
        var keys = [];
        spec.slice(hash + 1).split('.').forEach(function (token) {
            var m = /^([^\[\]]*)((?:\[\d+\])*)$/.exec(token);
            if (!m) { keys.push(token); return; }
            if (m[1] !== '') keys.push(m[1]);
            var indexes = m[2].match(/\d+/g);
            if (indexes) { for (var i = 0; i < indexes.length; i++) keys.push(indexes[i]); }
        });
        return { file: spec.slice(0, hash), keys: keys };
    }

    function writePath(root, keys, text) {
        if (!root || !keys.length) return false;
        var node = root;
        for (var i = 0; i < keys.length - 1; i++) {
            if (node === null || node === undefined) return false;
            node = node[keys[i]];
        }
        if (node === null || node === undefined) return false;
        var last = keys[keys.length - 1];
        if (!Object.prototype.hasOwnProperty.call(node, last)) return false;
        node[last] = text;
        return true;
    }

    function mapSource(mapId) {
        return 'Map' + ('000' + mapId).slice(-3) + '.json';
    }

    // ------------------------------------------------------------------ 主体

    function Translator() {
        this._language = null;
        this._tables = {};        // 语言 → { 路径: 译文 }
        this._byFile = {};        // 语言 → { 文件: [{spec, keys, text}] }
        this._snapshots = new WeakMap();  // 数据对象 → { 路径: 原文 }
        this._statics = [];       // 常驻的数据库对象 [{src, obj}]
        this._currentMap = null;  // 当前地图 {src, obj}
        this._mapId = null;
        this._ready = false;
        this._loading = false;
    }

    Translator.prototype.isInitialized = function () {
        return this._ready;
    };

    Translator.prototype.getCurrentLanguage = function () {
        return this._language || DEFAULT_LANGUAGE;
    };

    Translator.prototype.getAvailableLanguages = function () {
        return CONFIGURED.slice();
    };

    Translator.prototype.getLanguageLabel = function () {
        var code = this.getCurrentLanguage();
        return LANGUAGE_LABELS[code] || String(code).toUpperCase();
    };

    // 与事件脚本里的 World 机制无关，纯粹便于调试
    Translator.prototype.getStatus = function () {
        return {
            isInitialized: this._ready,
            currentLanguage: this.getCurrentLanguage(),
            availableLanguages: this.getAvailableLanguages(),
            loadedTables: Object.keys(this._tables),
            entryCount: (this._byFile[this.getCurrentLanguage()] ? 1 : 0)
        };
    };

    Translator.prototype.initialize = function () {
        if (this._ready || this._loading) return;
        this._loading = true;
        this._language = ConfigManager.language || DEFAULT_LANGUAGE;
        var self = this;
        this._loadTable(this._language, function (success) {
            self._loading = false;
            if (!success && self._language !== DEFAULT_LANGUAGE) {
                self._language = DEFAULT_LANGUAGE;
                self._loadTable(DEFAULT_LANGUAGE, function () {
                    self._ready = true;
                    self._applyAll();
                });
                return;
            }
            self._ready = true;
            self._applyAll();
        });
    };

    Translator.prototype._loadTable = function (language, callback) {
        if (this._tables[language]) { callback(true); return; }
        var xhr = new XMLHttpRequest();
        var self = this;
        xhr.open('GET', TABLE_PATH + language + '.json');
        xhr.overrideMimeType('application/json');
        xhr.onload = function () {
            if (xhr.status >= 400) { callback(false); return; }
            try {
                self._store(language, JSON.parse(xhr.responseText));
                callback(true);
            } catch (e) {
                console.error('gametrans: 译文表解析失败', language, e);
                callback(false);
            }
        };
        xhr.onerror = function () {
            console.warn('gametrans: 读不到译文表', TABLE_PATH + language + '.json');
            callback(false);
        };
        xhr.send();
    };

    Translator.prototype._store = function (language, table) {
        var byFile = {};
        for (var spec in table) {
            if (!Object.prototype.hasOwnProperty.call(table, spec)) continue;
            var parsed = parseSpec(spec);
            (byFile[parsed.file] = byFile[parsed.file] || []).push({
                spec: spec, keys: parsed.keys, text: table[spec]
            });
        }
        this._tables[language] = table;
        this._byFile[language] = byFile;
    };

    // ---- 观察引擎载入了哪些数据对象 -----------------------------------------

    Translator.prototype.observe = function (object) {
        if (!object) return;
        var src = null;
        if (typeof $dataMap !== 'undefined' && object === $dataMap) {
            var mapId = this._mapId;
            if (mapId === null && typeof $gameMap !== 'undefined' && $gameMap && $gameMap.mapId) {
                mapId = $gameMap.mapId();
            }
            if (mapId === null || mapId === undefined) return;
            src = mapSource(mapId);
            this._currentMap = { src: src, obj: object };
        } else {
            var files = DataManager._databaseFiles || [];
            for (var i = 0; i < files.length; i++) {
                if (window[files[i].name] === object) { src = files[i].src; break; }
            }
            if (!src) return;
            for (var j = 0; j < this._statics.length; j++) {
                if (this._statics[j].obj === object) break;
            }
            if (j === this._statics.length) this._statics.push({ src: src, obj: object });
        }

        if (object === $dataSystem && !this._ready && !this._loading) this.initialize();
        if (this._ready) this._applyTo(src, object);
        this._applyLocale();
    };

    // ---- 应用与还原 ---------------------------------------------------------

    Translator.prototype._snapshotOf = function (object) {
        var snap = this._snapshots.get(object);
        if (!snap) { snap = {}; this._snapshots.set(object, snap); }
        return snap;
    };

    Translator.prototype._applyTo = function (src, object) {
        var entries = (this._byFile[this._language] || {})[src];
        if (!entries) return;
        var snap = this._snapshotOf(object);
        for (var i = 0; i < entries.length; i++) {
            var entry = entries[i];
            // 快照只记一次：反复应用不能把译文当成原文存下来
            if (!Object.prototype.hasOwnProperty.call(snap, entry.spec)) {
                var original = readPath(object, entry.keys);
                if (original === undefined) continue;
                snap[entry.spec] = original;
            }
            writePath(object, entry.keys, entry.text);
        }
    };

    Translator.prototype._restoreAll = function () {
        var targets = this._statics.slice();
        if (this._currentMap) targets.push(this._currentMap);
        for (var i = 0; i < targets.length; i++) {
            var snap = this._snapshots.get(targets[i].obj);
            if (!snap) continue;
            for (var spec in snap) {
                if (!Object.prototype.hasOwnProperty.call(snap, spec)) continue;
                writePath(targets[i].obj, parseSpec(spec).keys, snap[spec]);
            }
        }
    };

    Translator.prototype._applyAll = function () {
        var targets = this._statics.slice();
        if (this._currentMap) targets.push(this._currentMap);
        for (var i = 0; i < targets.length; i++) {
            this._applyTo(targets[i].src, targets[i].obj);
        }
        this._applyLocale();
    };

    // locale 是引擎认的"当前语言"字段：改它，YEP_MessageCore 那类插件就会
    // 按 /^zh/ 之类的判断换字体。这不是我们的判据，只是把引擎的开关拨对。
    Translator.prototype._applyLocale = function () {
        if (typeof $dataSystem === 'undefined' || !$dataSystem) return;
        var code = this.getCurrentLanguage();
        if (!/^[A-Za-z]{2}(_[A-Za-z]{2})?$/.test(code)) return;
        if ($dataSystem.locale === code) return;
        $dataSystem.locale = code;
        if (typeof $gameSystem !== 'undefined' && $gameSystem
            && typeof $gameSystem.initMessageFontSettings === 'function') {
            $gameSystem.initMessageFontSettings();
        }
    };

    Translator.prototype.setLanguage = function (language) {
        if (!language) return;
        if (language === this._language && this._ready) return;
        var self = this;
        var switchTo = function () {
            if (self._ready) self._restoreAll();
            self._language = language;
            ConfigManager.language = language;
            if (self._ready) {
                self._applyAll();
                self._refreshAllWindows();
            }
        };
        if (this._tables[language]) { switchTo(); return; }
        this._loadTable(language, function (success) { if (success) switchTo(); });
    };

    Translator.prototype._refreshAllWindows = function () {
        if (typeof SceneManager !== 'undefined' && SceneManager._scene
            && typeof SceneManager._scene._refreshAllWindows === 'function') {
            SceneManager._scene._refreshAllWindows();
        }
    };

    function readPath(root, keys) {
        var node = root;
        for (var i = 0; i < keys.length; i++) {
            if (node === null || node === undefined) return undefined;
            node = node[keys[i]];
        }
        return node;
    }

    // ------------------------------------------------------------------ 装配

    window.$gametrans = new Translator();
    window.GametransTranslator = Translator;

    // 引擎载入每个数据对象（含地图）之后都会走这里
    var _DataManager_onLoad = DataManager.onLoad;
    DataManager.onLoad = function (object) {
        _DataManager_onLoad.call(this, object);
        $gametrans.observe(object);
    };

    // 记下正在载入哪张地图 —— 地图对象本身不带文件名，得靠这一步定位
    var _DataManager_loadMapData = DataManager.loadMapData;
    DataManager.loadMapData = function (mapId) {
        $gametrans._mapId = mapId;
        _DataManager_loadMapData.call(this, mapId);
    };

    // ---- 设置菜单：加一行、能存、切完刷新 --------------------------------

    var _Window_Options_makeCommandList = Window_Options.prototype.makeCommandList;
    Window_Options.prototype.makeCommandList = function () {
        _Window_Options_makeCommandList.call(this);
        if ($gametrans.getAvailableLanguages().length > 1) {
            this.addCommand(LANGUAGE_COMMAND, 'language');
        }
    };

    var _Window_Options_statusText = Window_Options.prototype.statusText;
    Window_Options.prototype.statusText = function (index) {
        if (this.commandSymbol(index) === 'language') {
            return '< ' + $gametrans.getLanguageLabel() + ' >';
        }
        return _Window_Options_statusText.call(this, index);
    };

    // **关键**：非语言行必须回到引擎自己的实现 —— 音量与开关的值是那里改的。
    var _Window_Options_processOk = Window_Options.prototype.processOk;
    Window_Options.prototype.processOk = function () {
        if (this.commandSymbol(this.index()) === 'language') {
            this.gametransCycleLanguage();
            return;
        }
        _Window_Options_processOk.call(this);
    };

    var _Window_Options_cursorRight = Window_Options.prototype.cursorRight;
    Window_Options.prototype.cursorRight = function () {
        if (this.commandSymbol(this.index()) === 'language') {
            this.gametransCycleLanguage();
            return;
        }
        _Window_Options_cursorRight.call(this);
    };

    var _Window_Options_cursorLeft = Window_Options.prototype.cursorLeft;
    Window_Options.prototype.cursorLeft = function () {
        if (this.commandSymbol(this.index()) === 'language') {
            this.gametransCycleLanguage();
            return;
        }
        _Window_Options_cursorLeft.call(this);
    };

    Window_Options.prototype.gametransCycleLanguage = function () {
        var languages = $gametrans.getAvailableLanguages();
        if (languages.length < 2) return;
        var index = languages.indexOf($gametrans.getCurrentLanguage());
        $gametrans.setLanguage(languages[(index + 1) % languages.length]);
        this.redrawCurrentItem();
        SoundManager.playCursor();
    };

    var _ConfigManager_makeData = ConfigManager.makeData;
    ConfigManager.makeData = function () {
        var config = _ConfigManager_makeData.call(this);
        config.language = $gametrans.getCurrentLanguage();
        return config;
    };

    var _ConfigManager_applyData = ConfigManager.applyData;
    ConfigManager.applyData = function (config) {
        _ConfigManager_applyData.call(this, config);
        if (config && config.language) {
            this.language = config.language;
            if ($gametrans.isInitialized()) $gametrans.setLanguage(config.language);
        }
    };

    var _Game_Interpreter_pluginCommand = Game_Interpreter.prototype.pluginCommand;
    Game_Interpreter.prototype.pluginCommand = function (command, args) {
        _Game_Interpreter_pluginCommand.call(this, command, args);
        if (command === 'GametransLanguage' && args.length > 0) {
            $gametrans.setLanguage(args[0]);
        }
    };

    // 有些插件（例如 DTextPicture）会找 TranslationManager：给一个同形状的门面，
    // 免得它们以为译文系统不存在。它按原文查表，我们把表按位置定键，
    // 所以这里只做"不改变行为"的转发，不假装能翻。
    if (typeof window.TranslationManager === 'undefined') {
        window.TranslationManager = {
            translateIfNeed: function (text, callback) {
                if (callback && typeof callback === 'function') callback(text);
                return text;
            },
            getCurrentLanguage: function () { return $gametrans.getCurrentLanguage(); },
            setLanguage: function (language) { return $gametrans.setLanguage(language); },
            getAvailableLanguages: function () { return $gametrans.getAvailableLanguages(); }
        };
    }
})();
