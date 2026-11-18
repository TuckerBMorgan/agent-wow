package modrt

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"path/filepath"
	"testing"
	"time"

	"github.com/agent-wow/agent-wow/pkg/modules/callback"
	"github.com/agent-wow/agent-wow/pkg/modules/discovery"
	"github.com/agent-wow/agent-wow/pkg/modules/internal/testutil"
	"github.com/agent-wow/agent-wow/pkg/modules/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protodesc"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"
	"google.golang.org/protobuf/types/descriptorpb"
	"google.golang.org/protobuf/types/dynamicpb"
	"google.golang.org/protobuf/types/known/anypb"
	"google.golang.org/protobuf/types/known/structpb"
)

func TestUnregisteredDescriptorsAndNullResults(t *testing.T) {
	root := t.TempDir()
	modtest.WriteModule(t, root, "caller", "dynamic")
	dir := modtest.WriteModule(t, root, "dynamic", "")
	set := &descriptorpb.FileDescriptorSet{File: []*descriptorpb.FileDescriptorProto{
		protodesc.ToFileDescriptorProto(structpb.File_google_protobuf_struct_proto),
		{Name: proto.String("unregistered.proto"), Package: proto.String("runtime.only"), Syntax: proto.String("proto3"), Dependency: []string{"google/protobuf/struct.proto"},
			MessageType: []*descriptorpb.DescriptorProto{{Name: proto.String("Request"), Field: []*descriptorpb.FieldDescriptorProto{{Name: proto.String("number"), Number: proto.Int32(1), Type: descriptorpb.FieldDescriptorProto_TYPE_UINT64.Enum(), Label: descriptorpb.FieldDescriptorProto_LABEL_OPTIONAL.Enum()}}}},
			Service: []*descriptorpb.ServiceDescriptorProto{{Name: proto.String("Dynamic"), Method: []*descriptorpb.MethodDescriptorProto{
				{Name: proto.String("Echo"), InputType: proto.String(".runtime.only.Request"), OutputType: proto.String(".runtime.only.Request")},
				{Name: proto.String("Null"), InputType: proto.String(".runtime.only.Request"), OutputType: proto.String(".google.protobuf.Value")},
			}}},
		},
	}}
	b, err := proto.Marshal(set)
	if err != nil {
		t.Fatal(err)
	}
	modtest.WriteFile(t, filepath.Join(dir, "module.pb"), b)
	modtest.WriteFile(t, filepath.Join(dir, "module.yaml"), []byte(`api_version: 1
enabled: true
compose: {file: compose.yaml, service: module}
grpc: {descriptor_set: module.pb}
rpc:
  echo: /runtime.only.Dynamic/Echo
  getNull: /runtime.only.Dynamic/Null
`))
	registry, err := moddisc.Load(root)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := protoregistry.GlobalTypes.FindMessageByName("runtime.only.Request"); err == nil {
		t.Fatal("test type was registered globally")
	}
	d, _ := registry.Lookup("dynamic")
	echo, _ := d.RPC("echo")
	arrived := make(chan time.Time, 1)
	canceled := make(chan struct{}, 1)
	detailed, err := status.New(codes.FailedPrecondition, "not initialized").WithDetails(structpb.NewStringValue("opaque module detail"))
	if err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer(grpc.UnknownServiceHandler(func(_ any, stream grpc.ServerStream) error {
		input := dynamicpb.NewMessage(echo.Descriptor().Input())
		if err := stream.RecvMsg(input); err != nil {
			return err
		}
		switch input.Get(input.Descriptor().Fields().ByNumber(1)).Uint() {
		case 42:
			return detailed.Err()
		case 43:
			deadline, _ := stream.Context().Deadline()
			arrived <- deadline
			<-stream.Context().Done()
			canceled <- struct{}{}
			return status.FromContextError(stream.Context().Err()).Err()
		}
		name, _ := grpc.MethodFromServerStream(stream)
		if name == "/runtime.only.Dynamic/Null" {
			return stream.SendMsg(structpb.NewNullValue())
		}
		return stream.SendMsg(input)
	}))
	go server.Serve(listener)
	defer server.Stop()
	conn, err := grpc.NewClient("passthrough:///fixture", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	m := New(registry, nil, nil)
	m.instances["dynamic"] = &instance{definition: d, conn: conn, healthy: true}
	defer m.Close()
	result, err := m.InvokeJSON(context.Background(), "dynamic.echo", json.RawMessage(`{"number":"18446744073709551615"}`))
	if err != nil || !bytes.Contains(result, []byte("18446744073709551615")) {
		t.Fatal(string(result), err)
	}
	result, err = m.InvokeJSON(context.Background(), "dynamic.getNull", nil)
	if err != nil || string(result) != "null" {
		t.Fatal(string(result), err)
	}

	// Exercise the same unregistered contract through the module-only gateway,
	// including binary Any payloads and rich target statuses.
	callbacks := bufconn.Listen(1 << 20)
	gateway := grpc.NewServer()
	modv1.RegisterSessionServer(gateway, modcb.New(m.ctx, "caller", modcb.Handlers{InvokeModule: m.invokeModule}))
	go gateway.Serve(callbacks)
	defer gateway.Stop()
	callbackConn, err := grpc.NewClient("passthrough:///callbacks", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return callbacks.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer callbackConn.Close()
	client := modv1.NewSessionClient(callbackConn)
	request := func(n uint64) *modv1.InvokeModuleRequest {
		input := dynamicpb.NewMessage(echo.Descriptor().Input())
		input.Set(input.Descriptor().Fields().ByNumber(1), protoreflect.ValueOfUint64(n))
		packed, err := anypb.New(input)
		if err != nil {
			t.Fatal(err)
		}
		return &modv1.InvokeModuleRequest{Module: "dynamic", Method: "echo", Request: packed}
	}
	input := request(^uint64(0))
	response, err := client.InvokeModule(context.Background(), input)
	if err != nil || !proto.Equal(response.GetResult(), input.Request) {
		t.Fatal(response, err)
	}
	_, err = client.InvokeModule(context.Background(), request(42))
	if !proto.Equal(status.Convert(err).Proto(), detailed.Proto()) {
		t.Fatal("target status or details changed", err)
	}
	if m.Err() != nil {
		t.Fatal("gameplay precondition killed session", m.Err())
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	finished := make(chan error, 1)
	go func() { _, err := client.InvokeModule(ctx, request(43)); finished <- err }()
	select {
	case deadline := <-arrived:
		outer, _ := ctx.Deadline()
		if deadline.IsZero() || deadline.After(outer.Add(20*time.Millisecond)) {
			t.Fatal("nested call reset deadline", deadline, outer)
		}
	case <-ctx.Done():
		t.Fatal("nested invocation did not arrive")
	}
	cancel()
	if err := <-finished; status.Code(err) != codes.Canceled {
		t.Fatal(err)
	}
	select {
	case <-canceled:
	case <-time.After(time.Second):
		t.Fatal("target did not observe cancellation")
	}
}
