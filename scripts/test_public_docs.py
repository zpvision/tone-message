"""Check documentation against the existing strict runtime contract."""
import json
from pathlib import Path
import unittest
import tempfile

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.batch_api import Envelope, Item, PATHS
from app.config import Settings
from app.main import create_app
from app.public_docs import public_schema


class PublicDocsTests(unittest.TestCase):
    def setUp(self):
        self.schema=public_schema(Settings())
        self.parts=self.schema['components']['schemas']

    def test_explicit_public_allowlist_and_bearer_only(self):
        self.assertEqual(set(self.schema['paths']),set(PATHS))
        self.assertEqual(self.schema['info']['title'],'FinKoper AI API')
        security=self.schema['components']['securitySchemes']
        self.assertEqual(set(security),{'BearerAuth'})
        self.assertEqual(security['BearerAuth']['type'],'http')
        self.assertEqual(security['BearerAuth']['scheme'],'bearer')
        for path in PATHS:
            self.assertEqual(set(self.schema['paths'][path]),{'post'})
            self.assertEqual(self.schema['paths'][path]['post']['security'],[{'BearerAuth':[]}])

    def test_request_example_is_real_envelope_with_serialized_batch(self):
        operation=self.schema['paths'][PATHS[0]]['post']
        value=operation['requestBody']['content']['application/json']['examples']['three_messages']['value']
        request=Envelope.model_validate(value)
        self.assertEqual(request.model,'qwen3:8b')
        self.assertFalse(request.stream)
        self.assertIsInstance(request.messages[-1].content,str)
        rows=json.loads(request.messages[-1].content)
        self.assertEqual([Item.model_validate(row).item for row in rows],[1,2,3])
        self.assertEqual(self.parts['UserMessage']['properties']['content']['type'],'string')
        self.assertFalse(self.parts['ChatCompletionRequest']['additionalProperties'])
        self.assertEqual(set(self.parts['ChatCompletionRequest']['properties']),{'model','stream','messages'})

    def test_integer_boolean_and_inner_schemas(self):
        item=self.parts['BatchItem']['properties']['item']
        self.assertEqual(item['type'],'integer')
        self.assertEqual(item['minimum'],1)
        for value in (True,False,1.0,'1'):
            with self.assertRaises(ValidationError):
                Item.model_validate({'item':value,'text':'x'})
        tone=self.parts['ToneItem']['properties']['tone']
        self.assertEqual(tone['type'],'integer')
        self.assertEqual(tone['enum'],[0,1,2])
        self.assertEqual(self.parts['BatchInput']['maxItems'],1000)

    def test_response_and_actual_error_shape(self):
        responses=self.schema['paths'][PATHS[0]]['post']['responses']
        response=responses['200']['content']['application/json']['example']
        content=response['choices'][0]['message']['content']
        self.assertIsInstance(content,str)
        self.assertEqual(json.loads(content),[{'item':1,'tone':2},{'item':2,'tone':1},{'item':3,'tone':0}])
        for status in (400,401,405,413,415,422,426,429,499,502,503,504):
            error=responses[str(status)]['content']['application/json']['example']
            self.assertEqual(set(error),{'error'})
            self.assertIn('request_id',error['error'])
            self.assertNotIn('detail',error)
        self.assertEqual(responses['429']['headers']['Retry-After']['schema']['example'],5)
        self.assertEqual(self.parts['AssistantMessage']['properties']['content']['type'],'string')

    def test_custom_fastapi_routes_and_no_inline_or_cdn_assets(self):
        with tempfile.TemporaryDirectory() as root:
            app=create_app(Settings(model_id="synthetic:1",inference_url="http://inference.test",api_enabled=True,cabinet_enabled=True,
                cabinet_data_dir=root+'/cabinet',api_data_dir=root+'/api'))
            with TestClient(app,base_url='https://testserver') as client:
                response=client.get('/openapi.json')
                self.assertEqual(response.status_code,200)
                self.assertEqual(set(response.json()['paths']),set(PATHS))
                self.assertEqual(client.get('/docs').status_code,200)

    def test_static_initialization_has_no_persistent_auth(self):
        root=Path(__file__).resolve().parents[1]/'app/static/swagger'
        initializer=(root/'initializer.js').read_text()
        self.assertIn('persistAuthorization: false',initializer)
        self.assertNotIn('localStorage',initializer)
        html=(root/'index.html').read_text()
        self.assertNotIn('<script>',html)
        self.assertNotIn('https://',html)
        self.assertIn('/static/swagger/swagger-ui-bundle.js',html)


if __name__=='__main__':
    unittest.main(verbosity=2)
