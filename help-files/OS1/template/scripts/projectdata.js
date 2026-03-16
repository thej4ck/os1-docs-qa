// Publish project specific data
(function() {
rh = window.rh;
model = rh.model;

rh.consts('DEFAULT_TOPIC', encodeURI("#html/index.htm".substring(1)));
rh.consts('HOME_FILEPATH', encodeURI("index.htm"));
rh.consts('START_FILEPATH', encodeURI('index.htm'));
rh.consts('HELP_ID', 'D961E01C-0BDA-4BD6-99A2-B07B28EE5EA5' || 'preview');
rh.consts('LNG_STOP_WORDS', ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "abbisognare", "alcuna", "alcune", "alcuni", "alcuno", "altra", "altre", "altri", "altro", "anche", "anziché", "avere", "ciascun", "ciascuna", "ciascuno", "come", "con", "cosa", "così", "da", "dentro", "di", "dopo", "dove", "dovere", "è", "e", "entrambe", "entrambi", "entro", "eppure", "era", "erano", "eri", "ero", "essere", "essi", "esso", "fa", "fare", "fatto", "già", "ha", "il", "in", "io", "lì", "loro", "lungo", "ma", "modo", "nessuna", "nessuno", "noi", "non", "o", "ok", "okay", "ottenere", "per", "perché", "più", "potere", "potrebbe", "quale", "quando", "quella", "quelle", "quelli", "quello", "questa", "queste", "questi", "questo", "qui", "quindi", "sarà", "sarai", "sarò", "se", "senza", "sono", "stato", "stessa", "stesso", "su", "tra", "troppo", "tu", "tuo", "tutte", "tutti", "un", "una", "uno", "usando", "usare", "usato", "vedere", "voi", "volere", "vostro"]);
rh.consts('LNG_SUBSTR_SEARCH', 0);

model.publish(rh.consts('KEY_DIR'), "ltr");
model.publish(rh.consts('KEY_LNG_NAME'), "it_IT");
model.publish(rh.consts('KEY_LNG'), {"Reset":"Reimposta","SearchResultsPerScreen":"Risultati ricerca per pagina","SyncToc":"SincSom","HomeButton":"Home page","WebSearchButton":"WebSearch","Welcome_header":"Benvenuto nel Centro assistenza","ApplyTip":"Applica","HighlightSearchResults":"Evidenzia risultati ricerca","GlossaryFilterTerms":"Trova termine","WebSearch":"WebSearch","Show":"Mostra","Welcome_text":"Come possiamo aiutarti oggi?","EnableAndSearch":"Mostra i risultati che includono tutte le parole cercate","ShowAll":"Mostra tutto","Next":">>","Print":"Stampa","NoScriptErrorMsg":"Per vedere questa pagina, attivare il supporto JavaScript nel browser.","PreviousLabel":"Precedente","Hide":"Nascondi","Search":"Cerca","Contents":"Argomenti","ShowHide":"Mostra/Nascondi","Canceled":"Annullato","favoritesLabel":"Preferiti","EndOfResults":"Fine dei risultati della ricerca.","Loading":"Caricamento in corso...","SidebarToggleTip":"Espandi/comprimi","ContentFilterChanged":"Il filtro del contenuto è cambiato, cerca di nuovo","Logo":"Logo","Logo/Author":"Prodotto da","JS_alert_LoadXmlFailed":"Errore: Impossibile caricare il file XML.","favoritesNameLabel":"Nome","Copyright":"© Copyright 2017. All rights reserved.","SearchTitle":"Cerca","Searching":"Ricerca in corso...","Disabled Next":">>","nofavoritesFound":"Non hai contrassegnato alcuna pagina come preferita.","unsetAsFavorite":"Elimina da Preferiti","Cancel":"Annulla","JS_alert_InitDatabaseFailed":"Errore: Impossibile inizializzare il database.","FilterIntro":"Seleziona il filtro:","ResultsFoundText":"Trovato/i %1 risultato/i per %2","UnknownError":"Errore sconosciuto","Seperate":"|","Index":"Indice analitico","setAsFavorite":"Imposta come preferito","setAsFavorites":"Aggiungi a Preferiti","TopicsNotFound":"Non è stato trovato nessun argomento.","SearchPageTitle":"Risultati ricerca","Glossary":"Glossario","SearchButtonTitle":"Cerca","Filter":"Filtra","HideAll":"Nascondi tutto","TableOfContents":"Sommario","NextLabel":"Successivo","Disabled Prev":"<<","Back":"Indietro","SearchOptions":"Opzioni di ricerca","OpenLinkInNewTab":"Apri in una nuova scheda","Prev":"<<","ShowTopicInContext":"Fai clic qui per vedere questa pagina nel contesto completo","FavoriteBoxTitle":"Preferiti","ToTopTip":"Torna all’inizio","NavTip":"Menu","IeCompatibilityErrorMsg":"Questa pagina non è visualizzabile in Explorer 8 e versioni precedenti.","IndexFilterKewords":"Trova parola chiave","JS_alert_InvalidExpression_1":"Il testo digitato non è un'espressione valida."});

model.publish(rh.consts('KEY_HEADER_DEFAULT_TITLE_COLOR'), "#ffffff");
model.publish(rh.consts('KEY_HEADER_DEFAULT_BACKGROUND_COLOR'), "#025172");
model.publish(rh.consts('KEY_LAYOUT_DEFAULT_FONT_FAMILY'), "\"Trebuchet MS\", Arial, sans-serif");

model.publish(rh.consts('KEY_HEADER_TITLE'), "OS1 - by OSItalia S.r.l.");
model.publish(rh.consts('KEY_HEADER_TITLE_COLOR'), "#FFB200");
model.publish(rh.consts('KEY_HEADER_BACKGROUND_COLOR'), "#003300");
model.publish(rh.consts('KEY_HEADER_LOGO_PATH'), "template/Azure_Blue/logo.png");
model.publish(rh.consts('KEY_LAYOUT_FONT_FAMILY'), "\"Trebuchet MS\", Arial, sans-serif");
model.publish(rh.consts('KEY_HEADER_HTML'), "<div class='topic-header' onClick='rh._.goToFullLayout()'>\
  <div class='logo'>\
    <img src='#{logo}' />\
  </div>\
  <div class='nav'>\
    <div class='title' title='#{title}'>\
      <span>#{title}</span>\
    </div>\
    <div class='gotohome' title='#{tooltip}'>\
      <span>#{label}</span>\
    </div></div>\
  </div>\
<div class='topic-header-shadow'></div>\
");
model.publish(rh.consts('KEY_HEADER_CSS'), ".topic-header { background-color: #{background-color}; color: #{color}; width: calc(100%); height: 3em; position: fixed; left: 0; top: 0; font-family: #{font-family}; display: table; box-sizing: border-box; }\
.topic-header-shadow { height: 3em; width: 100%; }\
.logo { cursor: pointer; padding: 0.2em; text-align: center; display: table-cell; vertical-align: middle; }\
.logo img { width: 1.875em; display: block; }\
.nav { width: 100%; display: table-cell; }\
.title { width: 40%; height: 100%; float: left; line-height: 3em; cursor: pointer; }\
.gotohome { width: 60%; float: left; text-align: right; height: 100%; line-height: 3em; cursor: pointer; }\
.title span, .gotohome span { padding: 0em 1em; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; display: block; }");

})();