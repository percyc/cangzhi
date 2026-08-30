window.__ModuleLoader__.load({
	id: "dsh-cangzhi",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
		let react_jsx_runtime = require("react/jsx-runtime");
		//#region \0dsh-css:/data/share/cangzhi/integrations/dsh-cangzhi/src/client/Cangzhi.module.css.mjs
		const css = ".HPdFfW_footer{align-items:center;width:100%;height:42px;margin-top:8px;display:flex}.HPdFfW_footerRail{width:36px;height:36px;margin:0}.HPdFfW_footerButton{width:calc(100% + 4px);height:42px;color:var(--dsw-alias-label-primary);font:inherit;cursor:pointer;background:0 0;border:none;border-radius:12px;align-items:center;gap:8px;margin:0 -2px;padding:0 10px 0 8px;display:inline-flex}.HPdFfW_footerButton:hover,.HPdFfW_footerButton[data-active=true]{background:var(--dsw-alias-interactive-bg-hover)}.HPdFfW_footerRail .HPdFfW_footerButton{border-radius:50%;justify-content:center;width:36px;height:36px;margin:0;padding:0}.HPdFfW_bookIcon{justify-content:center;align-items:center;width:18px;height:18px;font-size:16px;line-height:18px;display:inline-flex}.HPdFfW_footerLabel{text-overflow:ellipsis;white-space:nowrap;min-width:0;overflow:hidden}.HPdFfW_cangzhiMark{box-sizing:border-box;background:linear-gradient(145deg, var(--dsw-alias-brand-primary), #6b52d9);color:#fff;box-shadow:0 3px 10px color-mix(in srgb, var(--dsw-alias-brand-primary) 24%, transparent);border-radius:28%;flex:none;place-items:center;font-family:ui-serif,serif;font-weight:700;line-height:1;display:inline-grid}.HPdFfW_cangzhiBrandName{color:var(--dsw-alias-label-primary);align-items:baseline;gap:6px;display:inline-flex}.HPdFfW_cangzhiBrandName strong{font-size:15px;font-weight:650}.HPdFfW_cangzhiBrandName small{color:var(--dsw-alias-label-tertiary);letter-spacing:.08em;font-size:9px}.HPdFfW_homeIntegration{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);background:color-mix(in srgb, var(--dsw-alias-bg-base) 88%, var(--dsw-alias-brand-primary) 2%);width:100%;box-shadow:var(--dsw-shadow-lv1);border-radius:14px;padding:15px}.HPdFfW_homeLoading{color:var(--dsw-alias-label-tertiary);text-align:center;font-size:12px}.HPdFfW_homeIntro,.HPdFfW_homeTop{align-items:center;gap:11px;display:flex}.HPdFfW_homeTop{justify-content:space-between}.HPdFfW_homeIntro>div{flex-direction:column;min-width:0;display:flex}.HPdFfW_homeIntro strong{color:var(--dsw-alias-label-primary);font-size:14px}.HPdFfW_homeIntro small{color:var(--dsw-alias-label-tertiary);margin-top:3px;font-size:10px}.HPdFfW_homeLogin{grid-template-columns:1fr 1fr auto;gap:8px;margin-top:12px;display:grid}.HPdFfW_homeLogin input{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);min-width:0;height:34px;color:var(--dsw-alias-label-primary);border-radius:8px;padding:0 10px}.HPdFfW_homeLogin button,.HPdFfW_homeActions button{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-button-ghost-fill);height:34px;color:var(--dsw-alias-label-primary);font:inherit;cursor:pointer;border-radius:8px;padding:0 11px;font-size:11px}.HPdFfW_homeLogin button,.HPdFfW_homeActions .HPdFfW_homePrimary{background:var(--dsw-alias-brand-primary);color:#fff;border-color:#0000}.HPdFfW_homeLogin button:disabled,.HPdFfW_homeActions button:disabled{opacity:.5;cursor:default}.HPdFfW_homeNotice{color:var(--dsw-alias-label-secondary);margin:9px 0 0;font-size:10px}.HPdFfW_homeConnection{color:#bd7910;background:#e5a21a1f;border-radius:99px;flex:none;padding:5px 8px;font-size:9px}.HPdFfW_homeConnection[data-ok=true]{color:#138258;background:#1ca46f1f}.HPdFfW_homeStats{border-top:1px solid var(--dsw-alias-border-l2);border-bottom:1px solid var(--dsw-alias-border-l2);color:var(--dsw-alias-label-tertiary);gap:20px;margin:13px 0;padding:10px 0;font-size:10px;display:flex}.HPdFfW_homeStats strong{color:var(--dsw-alias-label-primary);margin-right:3px;font-size:14px}.HPdFfW_homeActions{gap:7px;display:flex}.HPdFfW_knowledgeDock{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);width:100%;color:var(--dsw-alias-label-secondary);border-radius:9px;align-items:center;gap:7px;margin-bottom:7px;padding:7px 9px;font-size:10px;display:flex}.HPdFfW_knowledgeDock>span:not(.HPdFfW_cangzhiMark){text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;overflow:hidden}.HPdFfW_knowledgeDock strong{color:var(--dsw-alias-label-primary);margin-right:5px}.HPdFfW_knowledgeDock button{color:var(--dsw-alias-brand-primary);font:inherit;cursor:pointer;background:0 0;border:0;font-size:10px}.HPdFfW_overlay{z-index:100;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-base);min-width:0;min-height:0;box-shadow:var(--dsw-shadow-lv3);border-radius:14px;flex-direction:column;display:flex;position:absolute;inset:10px;overflow:hidden}.HPdFfW_consoleHeader{border-bottom:1px solid var(--dsw-alias-border-l2);background:var(--dsw-specific-menu);flex:none;align-items:center;gap:10px;min-height:52px;padding:8px 10px 8px 14px;display:flex}.HPdFfW_consoleTitle{color:var(--dsw-alias-label-primary);flex:none;font-size:14px;font-weight:600}.HPdFfW_nativeBadge{min-width:0;color:var(--dsw-alias-label-tertiary);flex:1;font-size:12px}.HPdFfW_toolbarButton,.HPdFfW_closeButton{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-button-ghost-fill);height:34px;color:var(--dsw-alias-label-primary);font:inherit;cursor:pointer;border-radius:8px;flex:none;padding:0 11px;font-size:12px}.HPdFfW_toolbarButton:hover,.HPdFfW_closeButton:hover{background:var(--dsw-alias-interactive-bg-hover)}.HPdFfW_closeButton{width:34px;padding:0;font-size:18px}.HPdFfW_workspace{background:var(--dsw-alias-bg-layer-2);flex:1;min-height:0;display:flex}.HPdFfW_workspaceNav{box-sizing:border-box;border-right:1px solid var(--dsw-alias-border-l2);background:var(--dsw-specific-menu);flex-direction:column;flex:none;width:210px;padding:20px 12px;display:flex}.HPdFfW_brand{align-items:center;gap:10px;padding:0 10px 24px;display:flex}.HPdFfW_brand>span{background:var(--dsw-alias-brand-primary);color:#fff;border-radius:10px;place-items:center;width:34px;height:34px;font-size:19px;display:grid}.HPdFfW_brand div{flex-direction:column;min-width:0;display:flex}.HPdFfW_brand strong{color:var(--dsw-alias-label-primary);font-size:16px}.HPdFfW_brand small{color:var(--dsw-alias-label-tertiary);font-size:10px}.HPdFfW_workspaceNav>button{height:40px;color:var(--dsw-alias-label-secondary);text-align:left;font:inherit;cursor:pointer;background:0 0;border:0;border-radius:9px;margin-bottom:4px;padding:0 12px}.HPdFfW_workspaceNav>button:hover,.HPdFfW_workspaceNav>button[data-active=true]{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary);font-weight:600}.HPdFfW_connectionCard{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);border-radius:10px;align-items:center;gap:9px;margin-top:auto;padding:11px;display:flex}.HPdFfW_connectionCard>span{background:#e5a21a;border-radius:50%;width:8px;height:8px;box-shadow:0 0 0 3px #e5a21a2e}.HPdFfW_connectionCard>span[data-ok=true]{background:#1ca46f;box-shadow:0 0 0 3px #1ca46f2e}.HPdFfW_connectionCard div{flex-direction:column;min-width:0;display:flex}.HPdFfW_connectionCard strong{color:var(--dsw-alias-label-primary);font-size:11px}.HPdFfW_connectionCard small{color:var(--dsw-alias-label-tertiary);margin-top:2px;font-size:9px}.HPdFfW_workspaceMain{box-sizing:border-box;flex:1;min-width:0;padding:26px 30px 40px;overflow:auto}.HPdFfW_pageHeader{justify-content:space-between;align-items:center;margin-bottom:22px;display:flex}.HPdFfW_pageHeader h2{color:var(--dsw-alias-label-primary);margin:0;font-size:24px;line-height:30px}.HPdFfW_pageHeader p{color:var(--dsw-alias-label-tertiary);margin:3px 0 0;font-size:12px}.HPdFfW_headerActions{gap:7px;display:flex}.HPdFfW_refreshButton,.HPdFfW_rowActions button{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-button-ghost-fill);color:var(--dsw-alias-label-secondary);cursor:pointer;border-radius:8px;padding:7px 10px}.HPdFfW_stats{grid-template-columns:repeat(4,minmax(0,1fr));gap:13px;margin-bottom:16px;display:grid}.HPdFfW_stats article{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-base);border-radius:12px;flex-direction:column;min-height:115px;padding:16px;display:flex}.HPdFfW_stats small{color:var(--dsw-alias-label-tertiary);font-size:11px}.HPdFfW_stats strong{color:var(--dsw-alias-label-primary);margin-top:10px;font-size:28px;line-height:32px}.HPdFfW_stats span{color:var(--dsw-alias-label-secondary);margin-top:auto;font-size:10px}.HPdFfW_panel,.HPdFfW_uploadPanel{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-base);border-radius:12px;overflow:hidden}.HPdFfW_panelTitle{border-bottom:1px solid var(--dsw-alias-border-l2);justify-content:space-between;align-items:center;padding:15px 17px;display:flex}.HPdFfW_panelTitle h3{color:var(--dsw-alias-label-primary);margin:0;font-size:14px}.HPdFfW_panelTitle p{color:var(--dsw-alias-label-tertiary);margin:3px 0 0;font-size:10px}.HPdFfW_panelTitle button,.HPdFfW_primaryButton,.HPdFfW_categoryForm button{background:var(--dsw-alias-brand-primary);color:#fff;cursor:pointer;border:0;border-radius:8px;padding:8px 12px}.HPdFfW_documentRow{border-bottom:1px solid var(--dsw-alias-border-l2);align-items:center;gap:11px;min-height:55px;padding:0 16px;display:flex}.HPdFfW_documentRow:last-child{border-bottom:0}.HPdFfW_fileIcon{background:var(--dsw-alias-bg-layer-3);width:30px;height:30px;color:var(--dsw-alias-label-secondary);border-radius:7px;flex:none;place-items:center;display:grid}.HPdFfW_documentName{flex-direction:column;flex:1;min-width:0;display:flex}.HPdFfW_documentName strong{color:var(--dsw-alias-label-primary);text-overflow:ellipsis;white-space:nowrap;font-size:12px;overflow:hidden}.HPdFfW_documentName small{color:var(--dsw-alias-label-tertiary);margin-top:3px;font-size:9px}.HPdFfW_status{background:var(--dsw-alias-bg-layer-3);color:var(--dsw-alias-label-secondary);border-radius:99px;flex:none;padding:4px 7px;font-size:9px}.HPdFfW_status[data-status=completed],.HPdFfW_status[data-status=ready]{color:#1ca46f;background:#1ca46f24}.HPdFfW_status[data-status=processing]{background:color-mix(in srgb, var(--dsw-alias-brand-primary) 14%, transparent);color:var(--dsw-alias-brand-primary)}.HPdFfW_status[data-status=failed]{color:var(--dsw-alias-label-error)}.HPdFfW_rowActions{gap:5px;display:flex}.HPdFfW_rowActions button{padding:5px 7px;font-size:9px}.HPdFfW_libraryToolbar{border-bottom:1px solid var(--dsw-alias-border-l2);align-items:center;gap:10px;padding:13px 16px;display:flex}.HPdFfW_libraryToolbar input,.HPdFfW_categoryForm input,.HPdFfW_loginForm input{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);height:36px;color:var(--dsw-alias-label-primary);border-radius:8px;flex:1;padding:0 11px}.HPdFfW_libraryToolbar span{color:var(--dsw-alias-label-tertiary);font-size:10px}.HPdFfW_searchForm{border-bottom:1px solid var(--dsw-alias-border-l2);gap:8px;padding:14px;display:flex}.HPdFfW_searchForm input,.HPdFfW_createPanel input,.HPdFfW_createPanel textarea{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);font:inherit;border-radius:8px;flex:1;padding:0 11px}.HPdFfW_searchForm input,.HPdFfW_createPanel input{height:37px}.HPdFfW_searchForm button{background:var(--dsw-alias-brand-primary);color:#fff;cursor:pointer;border:0;border-radius:8px;padding:0 15px}.HPdFfW_searchMeta{color:var(--dsw-alias-label-tertiary);margin:0;padding:10px 15px;font-size:10px}.HPdFfW_searchResults article{border-top:1px solid var(--dsw-alias-border-l2);padding:13px 16px}.HPdFfW_searchResults article>div{flex-direction:column;display:flex}.HPdFfW_searchResults strong{color:var(--dsw-alias-label-primary);font-size:12px}.HPdFfW_searchResults small{color:var(--dsw-alias-label-tertiary);margin-top:3px;font-size:9px}.HPdFfW_searchResults p{color:var(--dsw-alias-label-secondary);white-space:pre-wrap;margin:8px 0 0;font-size:10px;line-height:16px}.HPdFfW_createPanel{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-base);border-radius:12px;padding:18px}.HPdFfW_modeTabs{gap:4px;margin-bottom:14px;display:flex}.HPdFfW_modeTabs button{color:var(--dsw-alias-label-secondary);cursor:pointer;background:0 0;border:0;border-radius:8px;padding:7px 12px}.HPdFfW_modeTabs button[data-active=true]{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary);font-weight:600}.HPdFfW_createPanel form{flex-direction:column;gap:10px;display:flex}.HPdFfW_createPanel textarea{resize:vertical;min-height:230px;padding-top:10px}.HPdFfW_createPanel .HPdFfW_primaryButton{align-self:flex-start}.HPdFfW_uploadPanel{padding:20px}.HPdFfW_dropZone{border:1px dashed var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);width:100%;min-height:210px;color:var(--dsw-alias-label-primary);cursor:pointer;border-radius:11px;flex-direction:column;justify-content:center;align-items:center;display:flex}.HPdFfW_dropZone>span{font-size:27px}.HPdFfW_dropZone strong{margin-top:10px;font-size:14px}.HPdFfW_dropZone small{color:var(--dsw-alias-label-tertiary);margin-top:6px}.HPdFfW_uploadList{max-height:180px;margin:13px 0;overflow:auto}.HPdFfW_uploadList>div{border-bottom:1px solid var(--dsw-alias-border-l2);align-items:center;gap:8px;min-height:35px;font-size:11px;display:flex}.HPdFfW_uploadList strong{color:var(--dsw-alias-label-primary);flex:1}.HPdFfW_uploadList small,.HPdFfW_progressText{color:var(--dsw-alias-label-tertiary);font-size:10px}.HPdFfW_primaryButton{margin-top:12px}.HPdFfW_primaryButton:disabled{opacity:.45;cursor:default}.HPdFfW_categoryForm{gap:8px;margin-bottom:15px;display:flex}.HPdFfW_categoryGrid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;display:grid}.HPdFfW_categoryGrid article{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-base);border-radius:10px;align-items:center;gap:10px;min-height:65px;padding:0 13px;display:flex}.HPdFfW_categoryGrid article>div{flex-direction:column;flex:1;min-width:0;display:flex}.HPdFfW_categoryGrid strong{color:var(--dsw-alias-label-primary);font-size:12px}.HPdFfW_categoryGrid small{color:var(--dsw-alias-label-tertiary);font-size:9px}.HPdFfW_categoryGrid button{color:var(--dsw-alias-label-secondary);cursor:pointer;background:0 0;border:0;font-size:9px}.HPdFfW_connectPanel{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-base);border-radius:12px;padding:22px}.HPdFfW_connectHero{background:var(--dsw-alias-bg-layer-2);border-radius:11px;align-items:center;gap:14px;padding:18px;display:flex}.HPdFfW_connectHero>span{color:#fff;background:#e5a21a;border-radius:13px;flex:none;place-items:center;width:44px;height:44px;font-size:21px;display:grid}.HPdFfW_connectHero>span[data-ok=true]{background:#1ca46f}.HPdFfW_connectHero h3{color:var(--dsw-alias-label-primary);margin:0;font-size:16px}.HPdFfW_connectHero p{max-width:650px;color:var(--dsw-alias-label-secondary);margin:5px 0 0;font-size:11px;line-height:17px}.HPdFfW_capabilityGrid{grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:16px 0;display:grid}.HPdFfW_capabilityGrid article{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);border-radius:9px;flex-direction:column;min-height:70px;padding:13px;display:flex}.HPdFfW_capabilityGrid strong{color:var(--dsw-alias-label-primary);font-size:11px}.HPdFfW_capabilityGrid small{color:var(--dsw-alias-label-tertiary);margin-top:5px;font-size:9px;line-height:14px}.HPdFfW_securityNote{color:var(--dsw-alias-label-tertiary);margin:16px 0 0;font-size:9px;line-height:15px}.HPdFfW_secondaryButton{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-button-ghost-fill);color:var(--dsw-alias-label-primary);cursor:pointer;border-radius:8px;margin-top:12px;padding:8px 12px}.HPdFfW_tokenList{border-top:1px solid var(--dsw-alias-border-l2);margin-top:18px;padding-top:14px}.HPdFfW_tokenList h4{color:var(--dsw-alias-label-primary);margin:0 0 8px;font-size:12px}.HPdFfW_tokenList>p{color:var(--dsw-alias-label-tertiary);font-size:10px}.HPdFfW_tokenList>div{border-bottom:1px solid var(--dsw-alias-border-l2);align-items:center;gap:9px;min-height:48px;display:flex}.HPdFfW_tokenList>div>div{flex-direction:column;flex:1;min-width:0;display:flex}.HPdFfW_tokenList strong{color:var(--dsw-alias-label-primary);font-size:10px}.HPdFfW_tokenList small{color:var(--dsw-alias-label-tertiary);text-overflow:ellipsis;white-space:nowrap;margin-top:3px;font-size:8px;overflow:hidden}.HPdFfW_tokenList span{color:#138258;background:#1ca46f1f;border-radius:99px;padding:3px 6px;font-size:8px}.HPdFfW_tokenList span[data-revoked=true]{background:var(--dsw-alias-bg-layer-3);color:var(--dsw-alias-label-tertiary)}.HPdFfW_tokenList button{color:var(--dsw-alias-label-error);cursor:pointer;background:0 0;border:0;font-size:9px}.HPdFfW_empty,.HPdFfW_centerState{min-height:160px;color:var(--dsw-alias-label-tertiary);place-items:center;font-size:12px;display:grid}.HPdFfW_errorBanner{background:color-mix(in srgb, var(--dsw-alias-label-error) 10%, transparent);color:var(--dsw-alias-label-error);border-radius:8px;margin:10px 0;padding:9px 11px;font-size:11px}.HPdFfW_loginPanel{background:var(--dsw-alias-bg-layer-2);flex-direction:column;flex:1;justify-content:center;align-items:center;min-height:0;display:flex}.HPdFfW_loginMark{background:var(--dsw-alias-brand-primary);color:#fff;border-radius:14px;place-items:center;width:48px;height:48px;font-size:24px;display:grid}.HPdFfW_loginPanel h2{color:var(--dsw-alias-label-primary);margin:14px 0 4px}.HPdFfW_loginPanel>p{color:var(--dsw-alias-label-tertiary);margin:0;font-size:11px}.HPdFfW_loginForm{flex-direction:column;gap:9px;width:280px;margin-top:18px;display:flex}.HPdFfW_loginForm input{flex:none}.HPdFfW_loginForm>button{background:var(--dsw-alias-brand-primary);color:#fff;cursor:pointer;border:0;border-radius:8px;height:37px}@media (width<=820px){.HPdFfW_workspaceNav{width:150px}.HPdFfW_stats{grid-template-columns:repeat(2,minmax(0,1fr))}.HPdFfW_categoryGrid,.HPdFfW_capabilityGrid{grid-template-columns:1fr}.HPdFfW_workspaceMain{padding:20px 16px}}.HPdFfW_toolCard{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);border-radius:10px;margin:4px 0;overflow:hidden}.HPdFfW_toolCard[data-state=error]{border-color:var(--dsw-alias-state-error-primary)}.HPdFfW_toolRow{align-items:center;gap:8px;min-height:38px;padding:0 10px;display:flex}.HPdFfW_toolGlyph{color:var(--dsw-alias-label-secondary);flex:none;font-size:13px}.HPdFfW_toolTitle{color:var(--dsw-alias-label-primary);flex:none;font-size:13px;font-weight:500}.HPdFfW_toolSummary{min-width:0;color:var(--dsw-alias-label-tertiary);text-overflow:ellipsis;white-space:nowrap;flex:1;font-size:12px;overflow:hidden}.HPdFfW_inspectButton{color:var(--dsw-alias-label-secondary);font:inherit;cursor:pointer;background:0 0;border:none;flex:none;padding:3px 5px;font-size:12px}.HPdFfW_toolPreview{color:var(--dsw-alias-label-secondary);margin:-2px 10px 9px 31px;font-size:12px;line-height:18px}.HPdFfW_toolDetails{color:var(--dsw-alias-label-tertiary);margin:0 10px 9px 31px;font-size:11px}.HPdFfW_toolOutput{background:var(--dsw-alias-bg-layer-3);max-height:260px;color:var(--dsw-alias-label-secondary);font-family:var(--dsh-font-mono,monospace);white-space:pre-wrap;word-break:break-word;border-radius:7px;margin:6px 0 0;padding:8px;font-size:11px;line-height:17px;overflow:auto}@media (width<=760px){.HPdFfW_overlay{border-radius:10px;inset:4px}.HPdFfW_consoleHeader{flex-wrap:wrap}.HPdFfW_consoleTitle{width:calc(100% - 44px)}.HPdFfW_urlForm{flex-basis:100%;order:2}.HPdFfW_frameHint{display:none}}";
		const tagId = "dsh-cangzhi/Cangzhi.module.css";
		if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId) + "]") === null) {
			const tag = document.createElement("style");
			tag.dataset.plugin = "dsh-cangzhi";
			tag.dataset.pluginCss = tagId;
			tag.textContent = css;
			document.head.appendChild(tag);
		}
		var Cangzhi_module_css_default = {
			"bookIcon": "HPdFfW_bookIcon",
			"brand": "HPdFfW_brand",
			"cangzhiBrandName": "HPdFfW_cangzhiBrandName",
			"cangzhiMark": "HPdFfW_cangzhiMark",
			"capabilityGrid": "HPdFfW_capabilityGrid",
			"categoryForm": "HPdFfW_categoryForm",
			"categoryGrid": "HPdFfW_categoryGrid",
			"centerState": "HPdFfW_centerState",
			"closeButton": "HPdFfW_closeButton",
			"connectHero": "HPdFfW_connectHero",
			"connectPanel": "HPdFfW_connectPanel",
			"connectionCard": "HPdFfW_connectionCard",
			"consoleHeader": "HPdFfW_consoleHeader",
			"consoleTitle": "HPdFfW_consoleTitle",
			"createPanel": "HPdFfW_createPanel",
			"documentName": "HPdFfW_documentName",
			"documentRow": "HPdFfW_documentRow",
			"dropZone": "HPdFfW_dropZone",
			"empty": "HPdFfW_empty",
			"errorBanner": "HPdFfW_errorBanner",
			"fileIcon": "HPdFfW_fileIcon",
			"footer": "HPdFfW_footer",
			"footerButton": "HPdFfW_footerButton",
			"footerLabel": "HPdFfW_footerLabel",
			"footerRail": "HPdFfW_footerRail",
			"frameHint": "HPdFfW_frameHint",
			"headerActions": "HPdFfW_headerActions",
			"homeActions": "HPdFfW_homeActions",
			"homeConnection": "HPdFfW_homeConnection",
			"homeIntegration": "HPdFfW_homeIntegration",
			"homeIntro": "HPdFfW_homeIntro",
			"homeLoading": "HPdFfW_homeLoading",
			"homeLogin": "HPdFfW_homeLogin",
			"homeNotice": "HPdFfW_homeNotice",
			"homePrimary": "HPdFfW_homePrimary",
			"homeStats": "HPdFfW_homeStats",
			"homeTop": "HPdFfW_homeTop",
			"inspectButton": "HPdFfW_inspectButton",
			"knowledgeDock": "HPdFfW_knowledgeDock",
			"libraryToolbar": "HPdFfW_libraryToolbar",
			"loginForm": "HPdFfW_loginForm",
			"loginMark": "HPdFfW_loginMark",
			"loginPanel": "HPdFfW_loginPanel",
			"modeTabs": "HPdFfW_modeTabs",
			"nativeBadge": "HPdFfW_nativeBadge",
			"overlay": "HPdFfW_overlay",
			"pageHeader": "HPdFfW_pageHeader",
			"panel": "HPdFfW_panel",
			"panelTitle": "HPdFfW_panelTitle",
			"primaryButton": "HPdFfW_primaryButton",
			"progressText": "HPdFfW_progressText",
			"refreshButton": "HPdFfW_refreshButton",
			"rowActions": "HPdFfW_rowActions",
			"searchForm": "HPdFfW_searchForm",
			"searchMeta": "HPdFfW_searchMeta",
			"searchResults": "HPdFfW_searchResults",
			"secondaryButton": "HPdFfW_secondaryButton",
			"securityNote": "HPdFfW_securityNote",
			"stats": "HPdFfW_stats",
			"status": "HPdFfW_status",
			"tokenList": "HPdFfW_tokenList",
			"toolCard": "HPdFfW_toolCard",
			"toolDetails": "HPdFfW_toolDetails",
			"toolGlyph": "HPdFfW_toolGlyph",
			"toolOutput": "HPdFfW_toolOutput",
			"toolPreview": "HPdFfW_toolPreview",
			"toolRow": "HPdFfW_toolRow",
			"toolSummary": "HPdFfW_toolSummary",
			"toolTitle": "HPdFfW_toolTitle",
			"toolbarButton": "HPdFfW_toolbarButton",
			"uploadList": "HPdFfW_uploadList",
			"uploadPanel": "HPdFfW_uploadPanel",
			"urlForm": "HPdFfW_urlForm",
			"workspace": "HPdFfW_workspace",
			"workspaceMain": "HPdFfW_workspaceMain",
			"workspaceNav": "HPdFfW_workspaceNav"
		};
		//#endregion
		//#region src/client/plugin.tsx
		const NS = "cangzhi";
		const API = "/_dsh-cangzhi-api";
		const TOOL_PREFIX = "mcp__cangzhi__";
		const RAW_TOOLS = [
			"knowledge_list_scopes",
			"knowledge_list_facets",
			"knowledge_list_documents",
			"knowledge_search",
			"knowledge_ask",
			"knowledge_get_document",
			"knowledge_get_chunk",
			"knowledge_list_datasets",
			"knowledge_get_dataset_schema",
			"knowledge_preview_dataset_rows",
			"knowledge_query_dataset",
			"knowledge_get_evidence_by_chunk",
			"knowledge_get_evidence_by_dataset",
			"knowledge_preview_evidence_rows"
		];
		const zh = {
			footer: "藏知",
			consoleTitle: "藏知管理中心",
			close: "关闭",
			frameHint: "DSH 原生知识工作台",
			running: "执行中…",
			done: "已完成",
			failed: "调用失败",
			items: "{{count}} 项",
			inspect: "检查调用",
			details: "查看原始结果",
			tools: {
				knowledge_list_scopes: "知识范围",
				knowledge_list_facets: "知识分类",
				knowledge_list_documents: "文档列表",
				knowledge_search: "知识搜索",
				knowledge_ask: "知识问答",
				knowledge_get_document: "读取文档",
				knowledge_get_chunk: "读取片段",
				knowledge_list_datasets: "数据集列表",
				knowledge_get_dataset_schema: "数据集结构",
				knowledge_preview_dataset_rows: "预览数据集",
				knowledge_query_dataset: "查询数据集",
				knowledge_get_evidence_by_chunk: "片段证据",
				knowledge_get_evidence_by_dataset: "数据集证据",
				knowledge_preview_evidence_rows: "预览证据"
			}
		};
		const en = {
			footer: "藏知",
			consoleTitle: "藏知管理中心",
			close: "Close",
			frameHint: "Native knowledge workspace for DSH",
			running: "Running…",
			done: "Completed",
			failed: "Call failed",
			items: "{{count}} items",
			inspect: "Inspect call",
			details: "Show raw result",
			tools: {
				knowledge_list_scopes: "Knowledge scopes",
				knowledge_list_facets: "Knowledge facets",
				knowledge_list_documents: "Documents",
				knowledge_search: "Knowledge search",
				knowledge_ask: "Knowledge answer",
				knowledge_get_document: "Read document",
				knowledge_get_chunk: "Read chunk",
				knowledge_list_datasets: "Datasets",
				knowledge_get_dataset_schema: "Dataset schema",
				knowledge_preview_dataset_rows: "Preview dataset",
				knowledge_query_dataset: "Query dataset",
				knowledge_get_evidence_by_chunk: "Chunk evidence",
				knowledge_get_evidence_by_dataset: "Dataset evidence",
				knowledge_preview_evidence_rows: "Preview evidence"
			}
		};
		function createConsoleFace() {
			let snapshot = { open: false };
			const listeners = /* @__PURE__ */ new Set();
			const publish = (next) => {
				if (next.open === snapshot.open) return;
				snapshot = next;
				for (const listener of listeners) listener();
			};
			return {
				hooks: { cangzhiConsole: {
					getSnapshot: () => snapshot,
					subscribe: (listener) => {
						listeners.add(listener);
						return () => {
							listeners.delete(listener);
						};
					}
				} },
				openConsole: () => {
					publish({
						...snapshot,
						open: true
					});
				},
				closeConsole: () => {
					publish({
						...snapshot,
						open: false
					});
				}
			};
		}
		function CangzhiMark({ size, className }) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
				className: `${Cangzhi_module_css_default.cangzhiMark} ${className ?? ""}`,
				style: {
					width: size,
					height: size,
					fontSize: Math.max(13, size * .55)
				},
				children: "知"
			});
		}
		function CangzhiBrandName() {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
				className: Cangzhi_module_css_default.cangzhiBrandName,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: "藏知" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "DSH" })]
			});
		}
		function CangzhiHeadline() {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(react_jsx_runtime.Fragment, { children: "和你的知识聊聊" });
		}
		function CangzhiDocumentTitle() {
			(0, react.useEffect)(() => {
				const update = () => {
					const next = document.title.replace(/DSH Local Build$/u, "藏知 DSH").replace(/DSH 本地构建$/u, "藏知 DSH");
					if (next !== document.title) document.title = next;
				};
				update();
				const observer = new MutationObserver(update);
				observer.observe(document.head, {
					childList: true,
					subtree: true,
					characterData: true
				});
				return () => {
					observer.disconnect();
				};
			}, []);
			return null;
		}
		function HomeIntegration({ openConsole }) {
			const [auth, setAuth] = (0, react.useState)(null);
			const [plugin, setPlugin] = (0, react.useState)(null);
			const [stats, setStats] = (0, react.useState)({
				documents: 0,
				categories: 0,
				processing: 0
			});
			const [username, setUsername] = (0, react.useState)("");
			const [password, setPassword] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const [notice, setNotice] = (0, react.useState)("");
			const fileInput = (0, react.useRef)(null);
			const load = async () => {
				const [authResponse, pluginResponse] = await Promise.all([fetch(`${API}/auth/status`, {
					credentials: "include",
					cache: "no-store"
				}), fetch("/_cangzhi-plugin/status", { cache: "no-store" })]);
				const authValue = await authResponse.json();
				setAuth(authValue);
				if (pluginResponse.ok) setPlugin(await pluginResponse.json());
				if (!authValue.authenticated) return;
				const [documentsResponse, categoriesResponse] = await Promise.all([fetch(`${API}/documents/overview?limit=200&offset=0&include_processing=true`, {
					credentials: "include",
					cache: "no-store"
				}), fetch(`${API}/categories`, {
					credentials: "include",
					cache: "no-store"
				})]);
				if (!documentsResponse.ok || !categoriesResponse.ok) return;
				const documents = await documentsResponse.json();
				const categories = await categoriesResponse.json();
				setStats({
					documents: Number(documentsResponse.headers.get("x-total-count") ?? documents.length),
					categories: categories.length,
					processing: documents.filter((item) => [
						"processing",
						"created",
						"retry"
					].includes(statusOf(item))).length
				});
			};
			(0, react.useEffect)(() => {
				load().catch(() => {
					setNotice("藏知服务暂时不可用");
				});
			}, []);
			const login = async (event) => {
				event.preventDefault();
				setBusy(true);
				setNotice("");
				const response = await fetch(`${API}/auth/login`, {
					method: "POST",
					credentials: "include",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({
						username,
						password
					})
				});
				if (!response.ok) {
					setNotice(await errorMessage(response, "登录失败"));
					setBusy(false);
					return;
				}
				setPassword("");
				if (plugin?.mcpConfigured) {
					setNotice("登录成功，藏知对话已经连接");
					await load();
					setBusy(false);
					return;
				}
				await connect();
			};
			const connect = async () => {
				setBusy(true);
				setNotice("正在创建 DSH 专用访问令牌…");
				const response = await fetch(`${API}/access-tokens`, {
					method: "POST",
					credentials: "include",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({
						name: "DSH 对话插件",
						scopes: [
							"knowledge:read",
							"knowledge:search",
							"knowledge:ask"
						]
					})
				});
				if (!response.ok) {
					setNotice(await errorMessage(response, "令牌创建失败"));
					setBusy(false);
					return;
				}
				const { token } = await response.json();
				const setup = await fetch("/_cangzhi-plugin/token", {
					method: "POST",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({ token })
				});
				setNotice(setup.ok ? "连接成功，藏知工具正在自动上线" : await errorMessage(setup, "DSH 凭据写入失败"));
				setBusy(false);
				await load();
			};
			const upload = async (files) => {
				if (!files?.length) return;
				setBusy(true);
				for (const [index, file] of Array.from(files).entries()) {
					setNotice(`正在上传 ${index + 1}/${files.length}：${file.name}`);
					const body = new FormData();
					body.append("file", file);
					body.append("title", "");
					const response = await fetch(`${API}/files/upload`, {
						method: "POST",
						credentials: "include",
						body
					});
					if (!response.ok) {
						setNotice(await errorMessage(response, `${file.name} 上传失败`));
						setBusy(false);
						return;
					}
				}
				setNotice("上传完成，已进入知识处理队列");
				setBusy(false);
				await load();
			};
			if (auth === null) return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("section", {
				className: Cangzhi_module_css_default.homeIntegration,
				children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: Cangzhi_module_css_default.homeLoading,
					children: "正在连接藏知知识库…"
				})
			});
			if (!auth.authenticated) return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: Cangzhi_module_css_default.homeIntegration,
				"data-state": "login",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: Cangzhi_module_css_default.homeIntro,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(CangzhiMark, { size: 38 }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: "连接藏知知识库" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "登录后，DSH 可以直接检索、引用和管理你的知识。" })] })]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("form", {
						className: Cangzhi_module_css_default.homeLogin,
						onSubmit: login,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: username,
								onChange: (event) => setUsername(event.target.value),
								placeholder: "藏知用户名",
								autoComplete: "username",
								required: true
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: password,
								onChange: (event) => setPassword(event.target.value),
								placeholder: "密码",
								type: "password",
								autoComplete: "current-password",
								required: true
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								disabled: busy,
								children: busy ? "登录中…" : "登录并连接"
							})
						]
					}),
					notice && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: Cangzhi_module_css_default.homeNotice,
						children: notice
					})
				]
			});
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: Cangzhi_module_css_default.homeIntegration,
				"data-state": "ready",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: Cangzhi_module_css_default.homeTop,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: Cangzhi_module_css_default.homeIntro,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(CangzhiMark, { size: 38 }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: "你的藏知知识库" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: plugin?.mcpConfigured ? "已接入当前 DSH 对话" : "管理服务已连接，尚未启用模型检索" })] })]
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: Cangzhi_module_css_default.homeConnection,
							"data-ok": String(Boolean(plugin?.mcpConfigured)),
							children: plugin?.mcpConfigured ? "对话已连接" : "等待认证"
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: Cangzhi_module_css_default.homeStats,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: stats.documents }), " 份资料"] }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: stats.categories }), " 个分类"] }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: stats.processing }), " 项处理中"] })
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: Cangzhi_module_css_default.homeActions,
						children: [
							!plugin?.mcpConfigured && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								className: Cangzhi_module_css_default.homePrimary,
								disabled: busy,
								onClick: () => void connect(),
								children: busy ? "连接中…" : "启用模型检索"
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								ref: fileInput,
								type: "file",
								accept: ".pdf,.doc,.docx,.xlsx,.xls,.md,.txt",
								multiple: true,
								hidden: true,
								onChange: (event) => void upload(event.target.files)
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								disabled: busy,
								onClick: () => fileInput.current?.click(),
								children: "上传知识"
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								onClick: openConsole,
								children: "管理知识库"
							})
						]
					}),
					notice && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: Cangzhi_module_css_default.homeNotice,
						children: notice
					})
				]
			});
		}
		function KnowledgeDock({ openConsole }) {
			const [status, setStatus] = (0, react.useState)(null);
			(0, react.useEffect)(() => {
				fetch("/_cangzhi-plugin/status", { cache: "no-store" }).then(async (response) => {
					if (response.ok) setStatus(await response.json());
				});
			}, []);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: Cangzhi_module_css_default.knowledgeDock,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(CangzhiMark, { size: 22 }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: "藏知" }), status?.mcpConfigured ? "已连接，可在对话中检索知识库" : "尚未认证，返回新对话首页完成连接"] }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						onClick: openConsole,
						children: "知识库"
					})
				]
			});
		}
		function ConsoleAction({ wide, useCangzhiConsole, openConsole, t }) {
			const state = useCangzhiConsole((value) => value);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: wide ? Cangzhi_module_css_default.footer : `${Cangzhi_module_css_default.footer} ${Cangzhi_module_css_default.footerRail}`,
				children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
					type: "button",
					className: Cangzhi_module_css_default.footerButton,
					"data-active": String(state.open),
					"aria-label": t("consoleTitle"),
					title: t("consoleTitle"),
					onClick: openConsole,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: Cangzhi_module_css_default.bookIcon,
						"aria-hidden": true,
						children: "▤"
					}), wide && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: Cangzhi_module_css_default.footerLabel,
						children: t("footer")
					})]
				})
			});
		}
		async function errorMessage(response, fallback) {
			const value = await response.json().catch(() => null);
			if (typeof value?.detail === "string") return value.detail;
			if (typeof value?.detail === "object" && typeof value.detail.message === "string") return value.detail.message;
			return fallback;
		}
		function LoginPanel({ onAuthenticated }) {
			const [username, setUsername] = (0, react.useState)("");
			const [password, setPassword] = (0, react.useState)("");
			const [error, setError] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const submit = async (event) => {
				event.preventDefault();
				setBusy(true);
				setError("");
				const response = await fetch(`${API}/auth/login`, {
					method: "POST",
					credentials: "include",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({
						username,
						password
					})
				});
				setBusy(false);
				if (!response.ok) {
					setError(await errorMessage(response, "登录失败"));
					return;
				}
				onAuthenticated();
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: Cangzhi_module_css_default.loginPanel,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: Cangzhi_module_css_default.loginMark,
						children: "▤"
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("h2", { children: "登录藏知" }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", { children: "登录后即可在 DSH 内上传、管理和维护知识库。" }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("form", {
						onSubmit: submit,
						className: Cangzhi_module_css_default.loginForm,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: username,
								onChange: (event) => setUsername(event.target.value),
								placeholder: "用户名",
								autoComplete: "username",
								required: true
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
								value: password,
								onChange: (event) => setPassword(event.target.value),
								placeholder: "密码",
								type: "password",
								autoComplete: "current-password",
								required: true
							}),
							error && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
								className: Cangzhi_module_css_default.errorBanner,
								children: error
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								disabled: busy,
								children: busy ? "登录中…" : "登录"
							})
						]
					})
				]
			});
		}
		function NativeWorkspace() {
			const [auth, setAuth] = (0, react.useState)(null);
			const [plugin, setPlugin] = (0, react.useState)(null);
			const [tab, setTab] = (0, react.useState)("overview");
			const [documents, setDocuments] = (0, react.useState)([]);
			const [categories, setCategories] = (0, react.useState)([]);
			const [total, setTotal] = (0, react.useState)(0);
			const [loading, setLoading] = (0, react.useState)(true);
			const [error, setError] = (0, react.useState)("");
			const [query, setQuery] = (0, react.useState)("");
			const refresh = async () => {
				setLoading(true);
				setError("");
				try {
					const authValue = await (await fetch(`${API}/auth/status`, {
						credentials: "include",
						cache: "no-store"
					})).json();
					setAuth(authValue);
					const pluginResponse = await fetch("/_cangzhi-plugin/status", { cache: "no-store" });
					if (pluginResponse.ok) setPlugin(await pluginResponse.json());
					if (authValue.authenticated) {
						const [documentResponse, categoryResponse] = await Promise.all([fetch(`${API}/documents/overview?limit=200&offset=0&include_processing=true`, {
							credentials: "include",
							cache: "no-store"
						}), fetch(`${API}/categories`, {
							credentials: "include",
							cache: "no-store"
						})]);
						if (!documentResponse.ok || !categoryResponse.ok) throw new Error("知识库读取失败");
						setDocuments(await documentResponse.json());
						setTotal(Number(documentResponse.headers.get("x-total-count") ?? 0));
						setCategories(await categoryResponse.json());
					}
				} catch (caught) {
					setError(caught instanceof Error ? caught.message : "藏知服务不可用");
				} finally {
					setLoading(false);
				}
			};
			(0, react.useEffect)(() => {
				refresh();
			}, []);
			const logout = async () => {
				await fetch(`${API}/auth/logout`, {
					method: "POST",
					credentials: "include"
				});
				setAuth({
					authenticated: false,
					admin: null
				});
			};
			if (loading && auth === null) return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: Cangzhi_module_css_default.centerState,
				children: "正在连接藏知…"
			});
			if (auth !== null && !auth.authenticated) return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(LoginPanel, { onAuthenticated: () => void refresh() });
			const visible = documents.filter((item) => item.title.toLowerCase().includes(query.trim().toLowerCase()));
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: Cangzhi_module_css_default.workspace,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("aside", {
					className: Cangzhi_module_css_default.workspaceNav,
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: Cangzhi_module_css_default.brand,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "▤" }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: "藏知" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "Knowledge for DSH" })] })]
						}),
						[
							["overview", "总览"],
							["search", "搜索知识"],
							["documents", "知识库"],
							["create", "链接与随手记"],
							["upload", "上传资料"],
							["categories", "分类管理"],
							["connect", "对话接入"]
						].map(([key, label]) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
							"data-active": String(tab === key),
							onClick: () => setTab(key),
							children: label
						}, key)),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: Cangzhi_module_css_default.connectionCard,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { "data-ok": String(Boolean(plugin?.mcpConfigured)) }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: plugin?.mcpConfigured ? "对话检索已连接" : "管理已连接" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: plugin?.mcpConfigured ? `${plugin.toolCount} 个模型工具可用` : "配置 PAT 后启用模型工具" })] })]
						})
					]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("main", {
					className: Cangzhi_module_css_default.workspaceMain,
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: Cangzhi_module_css_default.pageHeader,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("h2", { children: tab === "overview" ? "知识工作台" : tab === "search" ? "搜索知识" : tab === "documents" ? "知识库" : tab === "create" ? "链接与随手记" : tab === "upload" ? "上传资料" : tab === "categories" ? "分类管理" : "对话接入" }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("p", { children: ["你好，", auth?.admin?.username ?? "管理员"] })] }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: Cangzhi_module_css_default.headerActions,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									className: Cangzhi_module_css_default.refreshButton,
									onClick: () => void refresh(),
									children: "刷新"
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									className: Cangzhi_module_css_default.refreshButton,
									onClick: () => void logout(),
									children: "退出"
								})]
							})]
						}),
						error && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: Cangzhi_module_css_default.errorBanner,
							children: error
						}),
						tab === "overview" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Overview, {
							documents,
							categories,
							total,
							mcp: plugin?.mcpConfigured ?? false,
							go: setTab
						}),
						tab === "search" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(KnowledgeSearch, {}),
						tab === "documents" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Documents, {
							documents: visible,
							query,
							setQuery,
							refresh
						}),
						tab === "create" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(CreateKnowledge, {
							refresh,
							done: () => setTab("documents")
						}),
						tab === "upload" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Upload, {
							refresh,
							done: () => setTab("documents")
						}),
						tab === "categories" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Categories, {
							categories,
							refresh
						}),
						tab === "connect" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ConversationConnect, {
							configured: plugin?.mcpConfigured ?? false,
							refresh
						})
					]
				})]
			});
		}
		function Overview({ documents, categories, total, mcp, go }) {
			const processing = documents.filter((item) => [
				"processing",
				"created",
				"retry"
			].includes(item.pipeline?.overall_status ?? item.current_version?.processing_status ?? "")).length;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: Cangzhi_module_css_default.stats,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", { children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "知识资料" }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: total }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "当前工作区" })
					] }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", { children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "知识分类" }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: categories.length }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "持续整理中" })
					] }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", { children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "处理队列" }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: processing }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: processing ? "后台正在处理" : "队列空闲" })
					] }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", { children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "模型能力" }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: mcp ? "14" : "—" }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: mcp ? "对话工具在线" : "等待 PAT 配置" })
					] })
				]
			}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: Cangzhi_module_css_default.panel,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: Cangzhi_module_css_default.panelTitle,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("h3", { children: "最近资料" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", { children: "上传后自动解析、切片并进入检索" })] }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						onClick: () => go("upload"),
						children: "＋ 上传资料"
					})]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(DocumentRows, {
					documents: documents.slice(0, 8),
					refresh: () => Promise.resolve(),
					compact: true
				})]
			})] });
		}
		function statusOf(item) {
			return item.pipeline?.overall_status ?? item.current_version?.processing_status ?? "created";
		}
		const statusText = {
			completed: "已完成",
			ready: "已完成",
			processing: "处理中",
			created: "等待处理",
			retry: "等待重试",
			failed: "失败",
			unsupported: "未提取"
		};
		function DocumentRows({ documents, refresh, compact = false }) {
			const act = async (item, action) => {
				if (action === "delete" && !window.confirm(`将“${item.title}”移入回收站？`)) return;
				const response = await fetch(`${API}/documents/${item.id}${action === "reprocess" ? "/reprocess" : ""}`, {
					method: action === "reprocess" ? "POST" : "DELETE",
					credentials: "include"
				});
				if (!response.ok) window.alert(await errorMessage(response, "操作失败"));
				else await refresh();
			};
			if (!documents.length) return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: Cangzhi_module_css_default.empty,
				children: "还没有资料，先上传第一份知识吧。"
			});
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: Cangzhi_module_css_default.documentRows,
				children: documents.map((item) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: Cangzhi_module_css_default.documentRow,
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: Cangzhi_module_css_default.fileIcon,
							children: item.content_kind === "dataset" ? "▦" : "▤"
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: Cangzhi_module_css_default.documentName,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: item.title }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("small", { children: [
								item.primary_category?.name ?? "未分类",
								" · ",
								new Date(item.updated_at).toLocaleString()
							] })]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: Cangzhi_module_css_default.status,
							"data-status": statusOf(item),
							children: statusText[statusOf(item)] ?? statusOf(item)
						}),
						!compact && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: Cangzhi_module_css_default.rowActions,
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								onClick: () => void act(item, "reprocess"),
								children: "重新处理"
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								onClick: () => void act(item, "delete"),
								children: "删除"
							})]
						})
					]
				}, item.id))
			});
		}
		function Documents({ documents, query, setQuery, refresh }) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: Cangzhi_module_css_default.panel,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: Cangzhi_module_css_default.libraryToolbar,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
						value: query,
						onChange: (event) => setQuery(event.target.value),
						placeholder: "搜索资料名称…"
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", { children: [documents.length, " 条资料"] })]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(DocumentRows, {
					documents,
					refresh
				})]
			});
		}
		function KnowledgeSearch() {
			const [query, setQuery] = (0, react.useState)("");
			const [hits, setHits] = (0, react.useState)([]);
			const [backend, setBackend] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const [error, setError] = (0, react.useState)("");
			const search = async (event) => {
				event.preventDefault();
				setBusy(true);
				setError("");
				const response = await fetch(`${API}/search`, {
					method: "POST",
					credentials: "include",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({
						query: query.trim(),
						limit: 30,
						offset: 0
					})
				});
				if (!response.ok) {
					setError(await errorMessage(response, "搜索失败"));
					setBusy(false);
					return;
				}
				const body = await response.json();
				setHits(body.hits);
				setBackend(body.backend);
				setBusy(false);
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: Cangzhi_module_css_default.panel,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("form", {
						className: Cangzhi_module_css_default.searchForm,
						onSubmit: search,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
							value: query,
							onChange: (event) => setQuery(event.target.value),
							placeholder: "搜索标题、正文或知识片段",
							required: true
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
							disabled: busy,
							children: busy ? "搜索中…" : "搜索"
						})]
					}),
					error && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: Cangzhi_module_css_default.errorBanner,
						children: error
					}),
					backend && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("p", {
						className: Cangzhi_module_css_default.searchMeta,
						children: [
							hits.length,
							" 个结果 · ",
							backend
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: Cangzhi_module_css_default.searchResults,
						children: hits.map((hit, index) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: hit.title }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("small", { children: [
							hit.source_type,
							" · 匹配度 ",
							(hit.score * 100).toFixed(0),
							"% ",
							hit.categories?.map((item) => `· ${item.name}`).join("")
						] })] }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", { children: hit.snippet })] }, `${hit.document_id}:${index}`))
					}),
					!busy && backend && !hits.length && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: Cangzhi_module_css_default.empty,
						children: "没有找到匹配的知识。"
					})
				]
			});
		}
		function CreateKnowledge({ refresh, done }) {
			const [mode, setMode] = (0, react.useState)("note");
			const [title, setTitle] = (0, react.useState)("");
			const [content, setContent] = (0, react.useState)("");
			const [url, setUrl] = (0, react.useState)("");
			const [busy, setBusy] = (0, react.useState)(false);
			const [message, setMessage] = (0, react.useState)("");
			const submit = async (event) => {
				event.preventDefault();
				setBusy(true);
				setMessage("");
				const response = await fetch(mode === "note" ? `${API}/notes` : `${API}/sources/url`, {
					method: "POST",
					credentials: "include",
					headers: { "content-type": "application/json" },
					body: JSON.stringify(mode === "note" ? {
						title,
						content
					} : { url })
				});
				if (!response.ok) {
					setMessage(await errorMessage(response, "保存失败"));
					setBusy(false);
					return;
				}
				setMessage(mode === "note" ? "随手记已保存并进入处理队列" : "链接已收录并进入抓取队列");
				setTitle("");
				setContent("");
				setUrl("");
				setBusy(false);
				await refresh();
				window.setTimeout(done, 650);
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: Cangzhi_module_css_default.createPanel,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: Cangzhi_module_css_default.modeTabs,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						"data-active": String(mode === "note"),
						onClick: () => setMode("note"),
						children: "随手记"
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						"data-active": String(mode === "url"),
						onClick: () => setMode("url"),
						children: "网页链接"
					})]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("form", {
					onSubmit: submit,
					children: [
						mode === "note" ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
							value: title,
							onChange: (event) => setTitle(event.target.value),
							placeholder: "标题（可选）"
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("textarea", {
							value: content,
							onChange: (event) => setContent(event.target.value),
							placeholder: "记录想法、会议要点或任何需要沉淀的知识…",
							required: true
						})] }) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
							value: url,
							onChange: (event) => setUrl(event.target.value),
							placeholder: "https://example.com/article",
							type: "url",
							required: true
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
							className: Cangzhi_module_css_default.primaryButton,
							disabled: busy,
							children: busy ? "保存中…" : mode === "note" ? "保存随手记" : "收录链接"
						}),
						message && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
							className: Cangzhi_module_css_default.progressText,
							children: message
						})
					]
				})]
			});
		}
		function Upload({ refresh, done }) {
			const input = (0, react.useRef)(null);
			const [files, setFiles] = (0, react.useState)([]);
			const [busy, setBusy] = (0, react.useState)(false);
			const [progress, setProgress] = (0, react.useState)("");
			const supported = (0, react.useMemo)(() => ".pdf,.doc,.docx,.xlsx,.xls,.md,.txt", []);
			const upload = async () => {
				setBusy(true);
				for (let index = 0; index < files.length; index += 1) {
					setProgress(`正在上传 ${index + 1} / ${files.length}：${files[index].name}`);
					const body = new FormData();
					body.append("file", files[index]);
					body.append("title", "");
					const response = await fetch(`${API}/files/upload`, {
						method: "POST",
						credentials: "include",
						body
					});
					if (!response.ok) {
						window.alert(await errorMessage(response, `${files[index].name} 上传失败`));
						setBusy(false);
						return;
					}
				}
				setProgress("上传完成，资料已进入处理队列");
				setFiles([]);
				setBusy(false);
				await refresh();
				window.setTimeout(done, 700);
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: Cangzhi_module_css_default.uploadPanel,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
						ref: input,
						type: "file",
						accept: supported,
						multiple: true,
						hidden: true,
						onChange: (event) => setFiles(Array.from(event.target.files ?? []))
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
						className: Cangzhi_module_css_default.dropZone,
						onClick: () => input.current?.click(),
						disabled: busy,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "⇧" }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: "选择或拖入知识文件" }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "PDF、Word、Excel、Markdown、TXT，单次最多 50 个" })
						]
					}),
					files.length > 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: Cangzhi_module_css_default.uploadList,
						children: files.map((file) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "▤" }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: file.name }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("small", { children: [(file.size / 1024 / 1024).toFixed(2), " MB"] })
						] }, `${file.name}:${file.size}`))
					}),
					progress && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: Cangzhi_module_css_default.progressText,
						children: progress
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						className: Cangzhi_module_css_default.primaryButton,
						disabled: !files.length || busy,
						onClick: () => void upload(),
						children: busy ? "上传中…" : `上传 ${files.length || ""} 个文件`
					})
				]
			});
		}
		function Categories({ categories, refresh }) {
			const [name, setName] = (0, react.useState)("");
			const create = async (event) => {
				event.preventDefault();
				const response = await fetch(`${API}/categories`, {
					method: "POST",
					credentials: "include",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({ name })
				});
				if (!response.ok) window.alert(await errorMessage(response, "创建失败"));
				else {
					setName("");
					await refresh();
				}
			};
			const rename = async (item) => {
				const next = window.prompt("新的分类名称", item.name)?.trim();
				if (!next || next === item.name) return;
				const response = await fetch(`${API}/categories/${item.id}`, {
					method: "PATCH",
					credentials: "include",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({ name: next })
				});
				if (!response.ok) window.alert(await errorMessage(response, "重命名失败"));
				else await refresh();
			};
			const remove = async (item) => {
				if (!window.confirm(`删除分类“${item.name}”？`)) return;
				const response = await fetch(`${API}/categories/${item.id}`, {
					method: "DELETE",
					credentials: "include"
				});
				if (!response.ok) window.alert(await errorMessage(response, "删除失败"));
				else await refresh();
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("form", {
				className: Cangzhi_module_css_default.categoryForm,
				onSubmit: create,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
					value: name,
					onChange: (event) => setName(event.target.value),
					placeholder: "新分类名称",
					required: true
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", { children: "新增分类" })]
			}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("section", {
				className: Cangzhi_module_css_default.categoryGrid,
				children: categories.map((item) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", { children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "▤" }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: item.name }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("small", { children: [
						item.document_count,
						" 条资料 · ",
						item.slug
					] })] }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						onClick: () => void rename(item),
						children: "重命名"
					}),
					!item.is_default && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						onClick: () => void remove(item),
						children: "删除"
					})
				] }, item.id))
			})] });
		}
		function ConversationConnect({ configured, refresh }) {
			const [busy, setBusy] = (0, react.useState)(false);
			const [message, setMessage] = (0, react.useState)("");
			const [tokens, setTokens] = (0, react.useState)([]);
			const loadTokens = async () => {
				const response = await fetch(`${API}/access-tokens`, {
					credentials: "include",
					cache: "no-store"
				});
				if (response.ok) setTokens((await response.json()).items);
			};
			(0, react.useEffect)(() => {
				loadTokens();
			}, []);
			const connect = async () => {
				setBusy(true);
				setMessage("正在创建最小权限访问令牌…");
				const tokenResponse = await fetch(`${API}/access-tokens`, {
					method: "POST",
					credentials: "include",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({
						name: "DSH 对话插件",
						scopes: [
							"knowledge:read",
							"knowledge:search",
							"knowledge:ask"
						]
					})
				});
				if (!tokenResponse.ok) {
					setMessage(await errorMessage(tokenResponse, "访问令牌创建失败"));
					setBusy(false);
					return;
				}
				const created = await tokenResponse.json();
				setMessage("正在安全写入 DSH 凭据存储…");
				const setupResponse = await fetch("/_cangzhi-plugin/token", {
					method: "POST",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({ token: created.token })
				});
				if (!setupResponse.ok) {
					setMessage(await errorMessage(setupResponse, "DSH 凭据写入失败"));
					setBusy(false);
					return;
				}
				setMessage("连接成功，模型工具将在数秒内自动上线。");
				setBusy(false);
				await Promise.all([refresh(), loadTokens()]);
			};
			const disconnect = async () => {
				if (!window.confirm("断开 DSH 与藏知的对话连接？知识管理功能仍然可用。")) return;
				setBusy(true);
				const response = await fetch("/_cangzhi-plugin/token", { method: "DELETE" });
				setMessage(response.ok ? "已断开 DSH 对话连接" : await errorMessage(response, "断开失败"));
				setBusy(false);
				await refresh();
			};
			const revoke = async (id) => {
				if (!window.confirm("撤销这个藏知访问令牌？使用它的客户端会立即失效。")) return;
				const response = await fetch(`${API}/access-tokens/${id}/revoke`, {
					method: "POST",
					credentials: "include"
				});
				if (!response.ok) setMessage(await errorMessage(response, "撤销失败"));
				await loadTokens();
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: Cangzhi_module_css_default.connectPanel,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: Cangzhi_module_css_default.connectHero,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							"data-ok": String(configured),
							children: configured ? "✓" : "↗"
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("h3", { children: configured ? "DSH 对话已连接藏知" : "让大模型直接调用藏知" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", { children: configured ? "知识搜索、问答、文档读取和数据集查询工具已经注册到对话。" : "点击一次即可创建只读/检索/问答权限的独立令牌，并安全保存到 DSH 凭据存储。" })] })]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: Cangzhi_module_css_default.capabilityGrid,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: "知识检索" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "按范围、分类和文档召回证据" })] }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: "知识问答" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "基于藏知内容生成带引用答案" })] }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: "数据集查询" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "查看结构、预览并精确查询表格" })] })
						]
					}),
					!configured ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						className: Cangzhi_module_css_default.primaryButton,
						disabled: busy,
						onClick: () => void connect(),
						children: busy ? "正在连接…" : "启用 DSH 对话能力"
					}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						className: Cangzhi_module_css_default.secondaryButton,
						disabled: busy,
						onClick: () => void disconnect(),
						children: "断开对话连接"
					}),
					message && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: Cangzhi_module_css_default.progressText,
						children: message
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: Cangzhi_module_css_default.securityNote,
						children: "令牌仅在创建时从藏知传入 DSH，本页面不会显示或回读密钥；可随时在藏知的访问令牌管理中撤销。"
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: Cangzhi_module_css_default.tokenList,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("h4", { children: "藏知访问令牌" }), tokens.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", { children: "暂无访问令牌" }) : tokens.map((token) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", { children: token.name }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("small", { children: [
								token.token_prefix,
								"… · ",
								token.scopes.join("、")
							] })] }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								"data-revoked": String(Boolean(token.revoked_at)),
								children: token.revoked_at ? "已撤销" : "有效"
							}),
							!token.revoked_at && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								onClick: () => void revoke(token.id),
								children: "撤销"
							})
						] }, token.id))]
					})
				]
			});
		}
		function ConsoleOverlay({ useCangzhiConsole, closeConsole, t }) {
			if (!useCangzhiConsole((value) => value).open) return null;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: Cangzhi_module_css_default.overlay,
				role: "dialog",
				"aria-modal": "true",
				"aria-label": t("consoleTitle"),
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("header", {
					className: Cangzhi_module_css_default.consoleHeader,
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("strong", {
							className: Cangzhi_module_css_default.consoleTitle,
							children: t("consoleTitle")
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: Cangzhi_module_css_default.nativeBadge,
							children: t("frameHint")
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
							type: "button",
							className: Cangzhi_module_css_default.closeButton,
							"aria-label": t("close"),
							title: t("close"),
							onClick: closeConsole,
							children: "×"
						})
					]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(NativeWorkspace, {})]
			});
		}
		function argsRawOf(block) {
			return ("kind" in block ? block.call?.argsRaw : block.argsRaw) ?? "";
		}
		function resultTextOf(block) {
			if (!("kind" in block)) return null;
			const text = block.content.map((item) => item.type === "text" ? item.text : JSON.stringify(item)).join("\n");
			if (text.length > 0) return text;
			if (block.error === void 0) return null;
			return `${block.error.name}: ${block.error.code}`;
		}
		function parseObject(text) {
			try {
				const value = JSON.parse(text);
				return typeof value === "object" && value !== null && !Array.isArray(value) ? value : null;
			} catch {
				return null;
			}
		}
		function argumentSummary(raw) {
			const args = parseObject(raw);
			if (args === null) return raw.trim().slice(0, 160) || null;
			for (const key of [
				"question",
				"query",
				"sql",
				"document_id",
				"chunk_id",
				"dataset_id",
				"scope"
			]) {
				const value = args[key];
				if (typeof value === "string" && value.length > 0) return value.slice(0, 160);
			}
			return null;
		}
		function itemCount(value) {
			if (Array.isArray(value)) return value.length;
			if (typeof value !== "object" || value === null) return null;
			const object = value;
			for (const key of [
				"results",
				"hits",
				"documents",
				"chunks",
				"datasets",
				"rows",
				"items",
				"scopes",
				"facets",
				"evidence"
			]) if (Array.isArray(object[key])) return object[key].length;
			for (const key of ["count", "total"]) if (typeof object[key] === "number") return object[key];
			return null;
		}
		function resultPreview(value) {
			if (value === null) return null;
			for (const key of [
				"answer",
				"summary",
				"title",
				"name",
				"content",
				"text"
			]) {
				const candidate = value[key];
				if (typeof candidate === "string" && candidate.length > 0) return candidate.slice(0, 360);
			}
			return null;
		}
		function CangzhiToolCard({ toolName, block, inspect, t }) {
			const rawName = toolName.startsWith(TOOL_PREFIX) ? toolName.slice(14) : toolName;
			const output = resultTextOf(block);
			const value = output === null ? null : parseObject(output);
			const running = !("kind" in block);
			const failed = !running && block.isError;
			const count = value === null ? null : itemCount(value);
			const argument = argumentSummary(argsRawOf(block));
			const status = running ? t("running") : failed ? output?.split("\n", 1)[0] ?? t("failed") : count === null ? t("done") : t("items", { count });
			const preview = failed ? null : resultPreview(value);
			const title = t(`tools.${rawName}`);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: Cangzhi_module_css_default.toolCard,
				"data-tool": toolName,
				"data-state": failed ? "error" : running ? "running" : "ok",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: Cangzhi_module_css_default.toolRow,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: Cangzhi_module_css_default.toolGlyph,
								"aria-hidden": true,
								children: "▤"
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: Cangzhi_module_css_default.toolTitle,
								children: title
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: Cangzhi_module_css_default.toolSummary,
								children: argument === null ? status : `${argument} · ${status}`
							}),
							inspect !== void 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
								type: "button",
								className: Cangzhi_module_css_default.inspectButton,
								onClick: inspect,
								title: t("inspect"),
								children: "↗"
							})
						]
					}),
					preview !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: Cangzhi_module_css_default.toolPreview,
						children: preview
					}),
					output !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("details", {
						className: Cangzhi_module_css_default.toolDetails,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("summary", { children: t("details") }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
							className: Cangzhi_module_css_default.toolOutput,
							children: output.slice(0, 12e3)
						})]
					})
				]
			});
		}
		const inject = ["slots", "locale"];
		function apply(ctx) {
			ctx.effect(() => ctx.locale.register(NS, {
				zh,
				en
			}), "cangzhi: dictionaries");
			const consoleFace = createConsoleFace();
			ctx.slots.inject("sidebar.brand.mark", () => ctx.slots.register({
				name: "sidebar.brand.mark",
				id: "cangzhi-brand-mark",
				order: 0
			}, CangzhiMark));
			ctx.slots.inject("sidebar.brand.name", () => ctx.slots.register({
				name: "sidebar.brand.name",
				id: "cangzhi-brand-name",
				order: 0
			}, CangzhiBrandName));
			ctx.slots.inject("conversation.hero.brand.mark", () => ctx.slots.register({
				name: "conversation.hero.brand.mark",
				id: "cangzhi-hero-mark",
				order: 0
			}, CangzhiMark));
			ctx.slots.inject("conversation.hero.headline", () => ctx.slots.register({
				name: "conversation.hero.headline",
				id: "cangzhi-headline",
				order: 0
			}, CangzhiHeadline));
			ctx.slots.inject("conversation.hero.extension", () => ctx.slots.register({
				name: "conversation.hero.extension",
				id: "cangzhi-home",
				order: 10,
				inject: () => consoleFace
			}, HomeIntegration));
			ctx.slots.inject("conversation.input.dock", () => ctx.slots.register({
				name: "conversation.input.dock",
				id: "cangzhi-context",
				order: -20,
				inject: () => consoleFace
			}, KnowledgeDock));
			ctx.slots.inject("sidebar.footer.action", () => ctx.slots.register({
				name: "sidebar.footer.action",
				id: "cangzhi-console",
				order: 40,
				locale: NS,
				inject: () => consoleFace
			}, ConsoleAction));
			ctx.slots.inject("shell.overlay", () => ctx.slots.register({
				name: "shell.overlay",
				id: "cangzhi-console",
				order: 100,
				locale: NS,
				inject: () => consoleFace
			}, ConsoleOverlay));
			ctx.slots.inject("shell.overlay", () => ctx.slots.register({
				name: "shell.overlay",
				id: "cangzhi-document-title",
				order: -100
			}, CangzhiDocumentTitle));
			ctx.slots.inject("tool.call.toolview", function* () {
				for (const rawName of RAW_TOOLS) yield ctx.slots.register({
					name: "tool.call.toolview",
					key: `${TOOL_PREFIX}${rawName}`,
					locale: NS
				}, CangzhiToolCard);
			});
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map