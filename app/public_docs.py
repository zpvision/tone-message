"""Documentation only: explicit allowlist, no changes to runtime validation."""
import copy
import json
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse

from app.batch_api import PATHS


def ref(name):
    return {'$ref':'#/components/schemas/'+name}


def public_schema(settings):
    items=[{'item':1,'text':'Спасибо большое! Всё отлично работает.'},
           {'item':2,'text':'Когда будет готов отчёт?'},
           {'item':3,'text':'Ужасный сервис, проблема до сих пор не решена!'}]
    tones=[{'item':1,'tone':2},{'item':2,'tone':1},{'item':3,'tone':0}]
    request_example={'model':'qwen3:8b','stream':False,
                     'messages':[{'role':'user','content':json.dumps(items,ensure_ascii=False,separators=(',',':'))}]}
    response_example={'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(tones,separators=(',',':'))},'finish_reason':'stop'}],
                      'created':1792219974,'model':'qwen3:8b','object':'chat.completion',
                      'usage':{'prompt_tokens':3743,'completion_tokens':18,'total_tokens':3761}}
    integer={'type':'integer','minimum':1,'description':'Положительное целое JSON-число. Boolean запрещён. Backend также отклоняет 1.0/1e0 (строгий integer).','x-strict-integer':True}
    def obj(properties, required=None):
        return {'type':'object','additionalProperties':False,'required':required or list(properties),'properties':properties}
    schemas={
        'BatchItem':obj({'item':copy.deepcopy(integer),'text':{'type':'string','description':'Исходный текст без обрезания или нормализации. Пустой/пробельный → tone=1 без GPU.'}}),
        'ToneItem':obj({'item':copy.deepcopy(integer),'tone':{'type':'integer','enum':[0,1,2],'description':'0 — негатив, 1 — нейтрально, 2 — позитив. Boolean не допускается.'}}),
        'BatchInput':{'type':'array','maxItems':settings.api_max_items,'items':ref('BatchItem'),
                      'description':'Разобранное содержимое user.content. item уникальны (не обязательно последовательные); порядок сохраняется.',
                      'x-unique-by':'item','example':items},
        'BatchOutput':{'type':'array','items':ref('ToneItem'),'description':'Разобранное content ответа: каждый исходный item ровно один раз в исходном порядке.','example':tones},
        'UserMessage':obj({'role':{'type':'string','enum':['user']},'content':{'type':'string','contentMediaType':'application/json','contentSchema':ref('BatchInput'),
            'description':'СТРОКА, содержащая сериализованный JSON-массив item/text; не вложенный массив messages. Объекты только с item и text.',
            'example':request_example['messages'][0]['content']}}),
        'SystemMessage':obj({'role':{'type':'string','enum':['system']},'content':{'type':'string',
            'description':'Необязательные правила классификации. Отсутствие system включает закреплённый проверенный длинный FinKoper preset sentiment-v1. Переданный system заменяет его правила, не игнорируется.'}}),
        'Usage':obj({name:{'type':'integer','minimum':0} for name in ('prompt_tokens','completion_tokens','total_tokens')}),
        'AssistantMessage':obj({'role':{'type':'string','enum':['assistant']},'content':{'type':'string','contentMediaType':'application/json','contentSchema':ref('BatchOutput'),
            'description':'СТРОКА с JSON-массивом item/tone, без markdown. Разберите её через JSON.parse/json.loads.',
            'example':response_example['choices'][0]['message']['content']}}),
        'ApiError':obj({'error':obj({'code':{'type':'string'},'message':{'type':'string'},'request_id':{'type':'string'},
                                  'item':copy.deepcopy(integer),
                                   'context_budget':obj({
                                       'input_tokens':{'type':'integer','minimum':0},
                                       'output_reserve':{'type':'integer','minimum':0},
                                       'additional_reserve':{'type':'integer','minimum':0},
                                       'num_ctx':{'type':'integer','minimum':1},
                                       'method':{'type':'string'}
                                   })},['code','message','request_id'])})
    }
    schemas['ChatCompletionRequest']=obj({
        'model':{'type':'string','enum':list(dict.fromkeys(v for v in [settings.api_model_alias, settings.model_id, *settings.api_model_aliases] if v)),'description':'Совместимый идентификатор API; фактическая inference-модель задаётся сервером.'},
        'stream':{'type':'boolean','enum':[False],'default':False,'description':'Только non-streaming: false либо отсутствие поля.'},
        'messages':{'type':'array','minItems':1,'maxItems':2,'oneOf':[
            {'prefixItems':[ref('UserMessage')],'minItems':1,'maxItems':1},
            {'prefixItems':[ref('SystemMessage'),ref('UserMessage')],'minItems':2,'maxItems':2}],
            'description':'Ровно один user; перед ним разрешён ровно один system. История и другие роли не поддерживаются.'}},['model','messages'])
    schemas['Choice']=obj({'index':{'type':'integer','enum':[0]},'message':ref('AssistantMessage'),'finish_reason':{'type':'string','enum':['stop']}})
    schemas['ChatCompletion']=obj({'choices':{'type':'array','minItems':1,'maxItems':1,'items':ref('Choice')},
        'created':{'type':'integer','description':'Фактическое Unix-время формирования ответа.'},'model':{'type':'string','enum':list(dict.fromkeys(v for v in [settings.api_model_alias, settings.model_id, *settings.api_model_aliases] if v))},
        'object':{'type':'string','enum':['chat.completion']},'usage':ref('Usage')})
    schemas['Usage']['description']='Сумма фактических метрик внутренних запросов Ollama. System применяется к каждому непустому сообщению и учитывается каждый раз, в том числе cached input tokens. Не финансовый биллинг.'
    errors={
        400:('Некорректный JSON (внешний либо user.content), повторяющиеся ключи JSON.','invalid_batch_json'),
        401:('Ключ отсутствует, неверен или отозван. Basic Auth кабинета не является ключом API.','invalid_api_key'),
        405:('Разрешён только POST.','method_not_allowed'),
        413:(f'HTTP body больше {settings.api_max_body_bytes} байт (5 MiB); лимит Nginx и backend.','body_too_large'),
        415:('Требуется Content-Type: application/json.','unsupported_media_type'),
        422:('Ошибка типов/модели/ролей/неподдерживаемых полей; duplicate_item, invalid_item, batch_too_large, context_budget_exceeded. При ошибке элемента возвращается item. Весь пакет проверяется до GPU.','context_budget_exceeded'),
        426:('Требуется HTTPS. Никогда не отправляйте ключ на HTTP: уже отправленный ключ redirect не защищает.','https_required'),
        429:('Сервис занят, очередь переполнена или истекло время ожидания очереди (queue_timeout).','busy'),
        499:('Клиент отключился; внутренний статус отмены. Обычно клиент уже не может получить этот JSON-ответ.','client_disconnected'),
        502:('Сбой upstream, сетевой сбой, невалидный/незавершённый/schema ответ либо неполный собранный пакет. Частичный результат и фиктивный neutral не возвращаются.','invalid_inference_response'),
        503:('Inference/токенизатор/сервис недоступен.','inference_unavailable'),
        504:('Таймаут inference либо общий deadline пакета.','request_deadline'),
    }
    responses={'200':{'description':'Полный проверенный массив в СТРОКЕ content. Пустой массив → content="[]", usage=0.',
                      'content':{'application/json':{'schema':ref('ChatCompletion'),'example':response_example}}}}
    for status,(description,code) in errors.items():
        example={'error':{'code':code,'message':'Не удалось завершить обработку пакета','request_id':'example-request-id'}}
        if status==422:
            example['error']['item']=19
        responses[str(status)]={'description':description,'content':{'application/json':{'schema':ref('ApiError'),'example':example}},
                                'headers':{'X-Request-ID':{'schema':{'type':'string'},'description':'ID запроса для диагностики без содержимого сообщений.'}}}
    responses['429']['headers']['Retry-After']={'schema':{'type':'integer','minimum':1,'example':5},'description':'Число секунд перед повтором; сейчас 5. Повторы ограничивайте.'}
    responses['401']['headers']['WWW-Authenticate']={'schema':{'type':'string','example':'Bearer'}}
    operation={
        'tags':['Тональность'],'summary':'Пакетный анализ тональности',
        'description':f'''Один HTTP-запрос — один пакет; каждый text анализируется отдельно через Qwen3 8B.
Только non-streaming. Максимум {settings.api_max_items} сообщений и 5 MiB HTTP body.
Общий deadline {settings.api_deadline:g} секунд; пакет может выполняться несколько минут.
До первого GPU-вызова проверяется весь пакет, включая контекст {settings.api_num_ctx} и резерв ответа 128.
System можно опустить: используется закреплённый проверенный промпт FinKoper.
Переданный system заменяет правила классификации. Item/порядок сохраняет сервер.
При технической ошибке нет частичного массива или подстановки neutral.
Очередь: до {settings.api_max_waiting_batches} пакетов / {settings.api_max_waiting_messages} сообщений, ожидание {settings.api_queue_timeout:g} с; общий inference-lock с XLSX.
При disconnect/deadline следующие элементы не запускаются. Повтор может повторить вычисления; exactly-once не гарантируется.
contentSchema — описание JSON ВНУТРИ строки, не замена типа content.
После отключения клиента возможен внутренний статус 499/client_disconnected; клиент обычно уже не получает ответ.''',
        'security':[{'BearerAuth':[]}],
        'requestBody':{'required':True,'description':'model/stream/messages; дополнительные поля (включая temperature/top_p/max_tokens) не поддерживаются.',
                       'content':{'application/json':{'schema':ref('ChatCompletionRequest'),'examples':{
                           'three_messages':{'summary':'Три сообщения, закреплённый system по умолчанию','value':request_example}}}}},
        'responses':responses}
    paths={}
    for i,path in enumerate(PATHS):
        op=copy.deepcopy(operation)
        op['operationId']='batchSentiment' if i==0 else 'batchSentimentAlias'
        if i:
            op['summary']+=' (alias)'
        paths[path]={'post':op}
    return {'openapi':'3.1.0','info':{'title':'FinKoper AI API','version':'1.0.0',
            'description':'Пакетный анализ тональности сообщений через Qwen3 8B. Документация под Basic Auth кабинета; вызовы API — только Bearer. В Authorize вставляйте только API-ключ, без префикса Bearer. Авторизация не сохраняется в localStorage.'},
            'servers':[{'url':'https://ai.example.com'}],'paths':paths,
            'components':{'securitySchemes':{'BearerAuth':{'type':'http','scheme':'bearer',
                'description':'Независимый API-ключ FinKoper. Вставьте только ключ; Swagger добавит Bearer. Не пароль кабинета и не OAuth Сбера.'}},'schemas':schemas}}


def install_public_docs(application, settings):
    schema=public_schema(settings)
    application.openapi=lambda: copy.deepcopy(schema)

    @application.get('/openapi.json',include_in_schema=False)
    async def openapi_document():
        return JSONResponse(application.openapi())

    @application.get('/docs',include_in_schema=False)
    async def swagger():
        return FileResponse(Path(__file__).parent/'static/swagger/index.html')
