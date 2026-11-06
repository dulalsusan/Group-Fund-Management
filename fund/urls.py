from django.contrib.auth import views as auth_views
from django.urls import path

from . import views


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("post-login/", views.post_login, name="post_login"),
    path("client-info/", views.client_overview, name="client_overview"),
    path("register/", views.register, name="register"),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("pending/", views.pending, name="pending"),
    path("ledger/", views.ledger, name="ledger"),
    path("contributions/", views.contributions, name="contributions"),
    path("contributions/<int:contribution_id>/proof/", views.submit_contribution_proof, name="submit_contribution_proof"),
    path("contributions/<int:contribution_id>/receipt/", views.contribution_receipt, name="contribution_receipt"),
    path("contributions/<int:contribution_id>/verify/", views.verify_contribution, name="verify_contribution"),
    path("contributions/<int:contribution_id>/reject/", views.reject_contribution, name="reject_contribution"),
    path("contribution-penalties/<int:penalty_id>/collect/", views.collect_penalty, name="collect_penalty"),
    path("catch-up/proof/", views.submit_catch_up_proof, name="submit_catch_up_proof"),
    path("catch-up/<int:payment_id>/verify/", views.verify_catch_up_payment, name="verify_catch_up_payment"),
    path("loans/", views.loans, name="loans"),
    path("loans/request/", views.request_loan, name="request_loan"),
    path("loans/<int:loan_id>/disburse/", views.disburse, name="disburse_loan"),
    path("loans/<int:loan_id>/repay/", views.repay_loan, name="repay_loan"),
    path("decisions/", views.decisions, name="decisions"),
    path("decisions/<int:decision_id>/", views.decision_detail, name="decision_detail"),
    path("decisions/<int:decision_id>/vote/", views.vote, name="vote"),
    path("decisions/<int:decision_id>/signoff/", views.second_signoff, name="second_signoff"),
    path("decisions/<int:decision_id>/execute/", views.execute_decision, name="execute_decision"),
    path("documents/", views.documents, name="documents"),
    path("rulebook/", views.rulebook, name="rulebook"),
    path("files/<str:kind>/<int:object_id>/", views.private_file, name="private_file"),
    path("exit/", views.exit_request, name="exit_request"),
]
