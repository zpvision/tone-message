window.addEventListener('load', () => {
  window.ui = SwaggerUIBundle({
    url: '/openapi.json',
    dom_id: '#swagger-ui',
    deepLinking: true,
    persistAuthorization: false,
    validatorUrl: null,
    supportedSubmitMethods: ['post'],
    displayRequestDuration: true,
    presets: [SwaggerUIBundle.presets.apis],
    layout: 'BaseLayout'
  });
});
