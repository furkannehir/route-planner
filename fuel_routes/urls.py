from django.urls import path

from .views import demo, health, plan_route

urlpatterns = [
    path('health/', health, name='health'),
    path('routes/', plan_route, name='plan-route'),
    path('demo/', demo, name='demo'),
]
