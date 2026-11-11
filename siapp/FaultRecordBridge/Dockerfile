# Fault Record Bridge SIAPP  -  SIPROTEC fault records (COMTRADE / PQDIF / COMFEDE) -> MQTT/TLS
# Build with the SIAPP SDK:   python siapp-sdk/build.py -t docker FaultRecordBridge
#   -p a8000  -> linux/arm/v7 (CP-8031 / CP-8050)      -p sws -> linux/amd64 (SICAM S8000)
# Stage 1 compiles the Edge Data API shared object; stage 2 is a lean runtime (no compiler).

FROM python:3.12-alpine AS build
RUN apk add --no-cache g++
COPY edgedataapi /edgedataapi
RUN g++ -std=c++11 -O2 -shared -fPIC -Wno-psabi -I /edgedataapi/include \
        -o /edgedata.so /edgedataapi/src/edgedata.cpp
RUN pip install --no-cache-dir --prefix=/install paho-mqtt==2.1.0

FROM python:3.12-alpine
RUN apk add --no-cache libstdc++ libgcc ca-certificates tzdata \
 && rm -rf /usr/local/lib/python3.12/ensurepip /usr/local/lib/python3.12/idlelib \
           /usr/local/lib/python3.12/tkinter /usr/local/lib/python3.12/turtledemo \
           /usr/local/lib/python3.12/test /usr/local/lib/python3.12/lib2to3
COPY --from=build /install /usr/local
COPY --from=build /edgedata.so /edgedata.so
COPY src/edgedataapi.py /app/edgedataapi.py
COPY app/frbridge /app/frbridge
COPY config/frbridge.siapp.json /app/config.json
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app EDGEDATA_LIB=/edgedata.so
WORKDIR /app
ENTRYPOINT ["python3", "-m", "frbridge"]
