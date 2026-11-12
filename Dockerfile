FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal

RUN microdnf install -y python3 python3-pip shadow-utils && microdnf clean all
ARG EMULATOR_CHANNEL=LTS/latest
RUN curl -fSL "https://downloads.aws.com/aws-emulator/packages/aws-emulator/${EMULATOR_CHANNEL}/aws-emulator-$(uname -m).rpm" -o /tmp/aws-emulator.rpm \
    && sha256sum /tmp/aws-emulator.rpm > /opt/emulator-package.sha256 \
    && rpm -i /tmp/aws-emulator.rpm \
    && rm /tmp/aws-emulator.rpm \
    && useradd --create-home --uid 1000 emulator
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir boto3==1.40.75

RUN microdnf install -y nano && microdnf clean all
COPY lab.py handler.py consumer.json docker-shell.sh /opt/lab/
RUN chown -R emulator:emulator /opt/lab
USER emulator
ENV HOME=/home/emulator PATH=/opt/venv/bin:$PATH
WORKDIR /opt/lab
ENTRYPOINT ["bash", "/opt/lab/docker-shell.sh"]
